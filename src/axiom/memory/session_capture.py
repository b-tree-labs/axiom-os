# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Common cross-tool write path for session capture.

Convergence point for every conversation-turn write into the per-principal
memory ledger, regardless of which surface initiated it. Claude Code,
ChatGPT, Gemini, OpenCode, axi chat, the `axi memory record` CLI, and the
`axi memory ingest` backstop all call ``record_session_turn()`` so the
resulting fragment carries identical provenance, typing, and policy
enforcement.

Per spec-memory.md §1, conversation turns are memorable (they carry
provenance, may be projected into derived views, are subject to retraction
and retention). They route through ``CompositionService.write`` with
``cognitive_type="episodic"`` per spec-memory.md §3.2.

Provenance shape:

- ``principal_id`` — the human user. Per ADR-035 §D1, this becomes the
  ``accountable_human_id`` automatically when a human acts directly.
- ``agents`` — single-element set ``{f"{tool}:{model}"}`` (or ``{tool}``
  when model unknown). The ``tool`` distinguishes originating surface
  for cross-vendor scoping; the ``model`` records which model produced
  the assistant output.
- ``content.tool`` / ``content.model`` — also stored explicitly in
  fragment content for richer query/filter without parsing the agent
  string.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from axiom.memory.composition import CompositionService
    from axiom.memory.fragment import MemoryFragment


# ---------------------------------------------------------------------------
# Heartbeat — periodic write that confirms the memory write path is alive.
# ---------------------------------------------------------------------------


HEARTBEAT_FACT_KIND = "heartbeat"
HEARTBEAT_SOURCE_DEFAULT = "axi-monitor"
HEARTBEAT_OK_SECONDS = 60 * 60          # ≤ 60 min: ok
HEARTBEAT_WARN_SECONDS = 60 * 60 * 2    # 60–120 min: warn; >120 min: error


def record_heartbeat(
    *,
    composition: CompositionService,
    principal_id: str,
    source: str = HEARTBEAT_SOURCE_DEFAULT,
    event_time: str | None = None,
) -> MemoryFragment:
    """Write a single heartbeat fragment for ``principal_id``.

    Cron / launchd / systemd invokes ``axi memory heartbeat`` on a fixed
    cadence; ``axi dr`` flags missing/stale heartbeats per the
    OK/WARN/ERROR thresholds in :func:`heartbeat_freshness`.
    """
    now = event_time or datetime.now(timezone.utc).isoformat()
    content: dict[str, Any] = {
        "event_time": now,
        "fact_kind": HEARTBEAT_FACT_KIND,
        "source": source,
        "summary": f"heartbeat at {now}",
    }
    return composition.write(
        content=content,
        cognitive_type="episodic",
        principal_id=principal_id,
        agents={source},
        resources=set(),
    )


def heartbeat_freshness(
    *,
    composition: CompositionService,
    principal_id: str | None = None,
) -> dict[str, Any]:
    """Return the freshness state of the most-recent heartbeat fragment.

    Result keys:

    - ``state``: ``"ok"``, ``"warn"``, or ``"error"``
    - ``age_seconds``: seconds since the most recent heartbeat (or
      ``None`` when no heartbeat exists for this principal)
    - ``reason``: present when state != "ok"; one of ``"no_heartbeat"``,
      ``"stale_warn"``, ``"stale_error"``
    - ``most_recent_event_time``: ISO 8601 of the most recent heartbeat
      (or ``None``)

    ``principal_id`` falls back to the pinned default; raises if neither
    is set.
    """
    from axiom.memory.session_summary import list_fragments_by_principal

    resolved = resolve_principal_id(principal_id)

    fragments = list_fragments_by_principal(
        composition, resolved, limit=200,
    )
    heartbeats = [
        f for f in fragments
        if f.content.get("fact_kind") == HEARTBEAT_FACT_KIND
    ]
    if not heartbeats:
        return {
            "state": "error",
            "age_seconds": None,
            "reason": "no_heartbeat",
            "most_recent_event_time": None,
            "principal_id": resolved,
        }

    # Use content.event_time when present (the wall-clock heartbeat time);
    # fall back to provenance.timestamp (write time).
    def _ts(f) -> str:
        return f.content.get("event_time") or f.provenance.timestamp

    most_recent = max(heartbeats, key=_ts)
    ts_str = _ts(most_recent)
    when = datetime.fromisoformat(ts_str)
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    age = (datetime.now(timezone.utc) - when).total_seconds()

    if age <= HEARTBEAT_OK_SECONDS:
        state = "ok"
        reason = ""
    elif age <= HEARTBEAT_WARN_SECONDS:
        state = "warn"
        reason = "stale_warn"
    else:
        state = "error"
        reason = "stale_error"

    return {
        "state": state,
        "age_seconds": age,
        "reason": reason,
        "most_recent_event_time": ts_str,
        "principal_id": resolved,
    }


def resolve_principal_id(explicit: str | None) -> str:
    """Resolve principal_id with fallback to the pinned default.

    Order: explicit caller value (truthy) → ``memory.default_principal``
    setting → raise ValueError with a fix-hint pointing at
    ``axi settings set memory.default_principal``.

    The fallback eliminates the silent-cross-identity footgun where
    callers either omit ``--principal`` (and write under nothing) or
    pass a system-provided email that doesn't match the user's canonical
    ledger principal. Per ``feedback_axi_memory_principal.md``.
    """
    if explicit:
        return explicit

    # Lazy import — settings store lives in an extension; importing it at
    # module load time would couple the memory module to extension boot.
    from axiom.extensions.builtins.settings.store import SettingsStore

    pinned = SettingsStore().get("memory.default_principal", "")
    if pinned:
        return pinned

    raise ValueError(
        "principal_id required and no default pinned. Either pass "
        "--principal <id> on the command line, or pin a default with: "
        "`axi settings set --global memory.default_principal <id>`"
    )


def record_session_turn(
    *,
    composition: CompositionService,
    principal_id: str,
    tool: str,
    user_input: str,
    assistant_output: str,
    model: str | None = None,
    summary: str | None = None,
    scope: str = "user",
    event_time: str | None = None,
    extra: dict[str, Any] | None = None,
) -> MemoryFragment:
    """Record a single conversation turn into the principal's memory ledger.

    Parameters
    ----------
    composition
        Per-principal CompositionService — already wired with the user's
        ledger paths, signing key, and policy.
    principal_id
        The human user who owns this turn (e.g. ``"user@example.org"``).
    tool
        Originating surface — ``"claude-code"``, ``"chatgpt"``, ``"gemini"``,
        ``"opencode"``, ``"axi-chat"``, etc. Distinguishes which tool was
        used; surfaces in ``content.tool`` and in the ``agents`` set.
    user_input
        Raw user prompt for this turn (full text or a meaningful slice).
    assistant_output
        Raw assistant response text. May be empty for partial-turn writes
        (e.g. tool-call-only turns).
    model
        Model identifier (``"opus-4-7"``, ``"gpt-4"``, ``"gemini-2-flash"``).
        Optional; when omitted the agent id is just ``tool``.
    summary
        Compact summary suitable for prompt-injection in future turns.
        Auto-generated from inputs if not supplied.
    scope
        Logical scope — defaults to ``"user"`` (personal cross-tool memory).
        Extensions/classrooms override.
    event_time
        ISO 8601 timestamp; defaults to now (UTC).
    extra
        Free-form metadata stored under ``content.extra`` (session_id,
        host process, working directory, etc.).

    Returns the persisted MemoryFragment.
    """
    now = event_time or datetime.now(timezone.utc).isoformat()
    agent_id = f"{tool}:{model}" if model else tool

    content: dict[str, Any] = {
        "event_time": now,
        "scope": scope,
        "fact_kind": "chat_turn",
        "tool": tool,
        "model": model or "",
        "user_input": user_input,
        "assistant_output": assistant_output,
        "summary": summary if summary is not None else _default_summary(
            user_input, assistant_output,
        ),
    }
    if extra:
        content["extra"] = dict(extra)

    return composition.write(
        content=content,
        cognitive_type="episodic",
        principal_id=principal_id,
        agents={agent_id},
        resources=set(),
    )


def _default_summary(user_input: str, assistant_output: str) -> str:
    """Compact one-line summary suitable for session_summary prompt injection.

    Keeps the first ~80 chars of each side; LLM-summarized rollups can plug
    in later via the same call signature without changing call sites.
    """
    u = (user_input or "").strip().replace("\n", " ")[:80]
    a = (assistant_output or "").strip().replace("\n", " ")[:80]
    if not u and not a:
        return ""
    if not a:
        return f"User: {u}"
    if not u:
        return f"Assistant: {a}"
    return f"User: {u} → Assistant: {a}"


# ---------------------------------------------------------------------------
# Session-log ingest — lossless backstop for tools whose transcripts are
# accessible on disk (Claude Code today; others as their formats stabilize).
# ---------------------------------------------------------------------------


def parse_claude_code_jsonl(path: str) -> list[dict]:
    """Parse a Claude Code session JSONL into a list of turn-pair dicts.

    Pairs each ``type=user`` line with the next ``type=assistant`` line in
    the same file. Returns dicts with keys: ``user_input``,
    ``assistant_output``, ``model``, ``user_uuid``, ``assistant_uuid``,
    ``timestamp``, ``session_id``, ``cwd``, ``git_branch``, ``version``.

    Non-conversation lines (permission-mode, file-history-snapshot,
    attachment, ai-title, queue-operation, system, last-prompt) are
    ignored; an unmatched user (no following assistant) is dropped.

    No idempotency here — the caller decides whether to re-ingest.
    """
    import json as _json

    pairs: list[dict] = []
    pending_user: dict | None = None

    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = _json.loads(line)
            except _json.JSONDecodeError:
                continue

            t = rec.get("type")
            if t == "user":
                pending_user = rec
            elif t == "assistant" and pending_user is not None:
                pairs.append(_build_turn_pair(pending_user, rec))
                pending_user = None
    return pairs


def _build_turn_pair(user_rec: dict, assistant_rec: dict) -> dict:
    """Assemble a turn-pair dict from a user + assistant JSONL record pair."""
    user_msg = user_rec.get("message") or {}
    assistant_msg = assistant_rec.get("message") or {}

    user_content = user_msg.get("content")
    if isinstance(user_content, str):
        user_input = user_content
    elif isinstance(user_content, list):
        user_input = "\n\n".join(
            block.get("text", "")
            for block in user_content
            if isinstance(block, dict) and block.get("type") == "text"
        )
    else:
        user_input = ""

    assistant_content = assistant_msg.get("content")
    if isinstance(assistant_content, str):
        assistant_output = assistant_content
    elif isinstance(assistant_content, list):
        assistant_output = "\n\n".join(
            block.get("text", "")
            for block in assistant_content
            if isinstance(block, dict) and block.get("type") == "text"
        )
    else:
        assistant_output = ""

    # Model id comes from the assistant message; strip any anthropic-style
    # "claude-" prefix decoration if you want a tighter agent label, but
    # passing the full id through preserves provenance fidelity.
    model = assistant_msg.get("model") or ""

    return {
        "user_input": user_input,
        "assistant_output": assistant_output,
        "model": model,
        "user_uuid": user_rec.get("uuid", ""),
        "assistant_uuid": assistant_rec.get("uuid", ""),
        "timestamp": user_rec.get("timestamp", ""),
        "session_id": user_rec.get("sessionId", ""),
        "cwd": user_rec.get("cwd", ""),
        "git_branch": user_rec.get("gitBranch", ""),
        "version": user_rec.get("version", ""),
    }


def ingest_claude_code_jsonl(
    *,
    composition: CompositionService,
    path: str,
    principal_id: str,
    dry_run: bool = False,
    limit: int | None = None,
) -> dict:
    """Fold a Claude Code session JSONL into the principal's ledger.

    Each turn pair becomes one episodic fragment via ``record_session_turn``.
    The originating tool is ``claude-code`` and ``content.extra`` carries
    the source uuid + session id + cwd + git branch for provenance.

    Idempotency: scans existing fragments for principal_id and skips any
    turn pair whose ``user_uuid`` already appears in the ledger as
    ``content.extra.source_uuid``. Re-running ingest on the same
    transcript is a no-op; running on a transcript that grew in place
    writes only the new turn pairs.

    Returns ``{"scanned": N, "written": M, "skipped": K}``. ``dry_run=True``
    leaves ``written`` at 0 (and counts everything as scanned, none skipped
    since nothing's evaluated against the ledger).
    """
    pairs = parse_claude_code_jsonl(path)
    if limit is not None:
        pairs = pairs[:limit]

    if dry_run:
        return {"scanned": len(pairs), "written": 0, "skipped": 0}

    seen_uuids = _existing_source_uuids(composition, principal_id)

    written = 0
    skipped = 0
    for pair in pairs:
        uuid = pair.get("user_uuid", "")
        if uuid and uuid in seen_uuids:
            skipped += 1
            continue
        record_session_turn(
            composition=composition,
            principal_id=principal_id,
            tool="claude-code",
            model=pair["model"] or None,
            user_input=pair["user_input"],
            assistant_output=pair["assistant_output"],
            event_time=pair["timestamp"] or None,
            extra={
                "source_uuid": uuid,
                "assistant_uuid": pair["assistant_uuid"],
                "session_id": pair["session_id"],
                "cwd": pair["cwd"],
                "git_branch": pair["git_branch"],
                "version": pair["version"],
            },
        )
        if uuid:
            seen_uuids.add(uuid)  # protect against same-pair repeats in one ingest
        written += 1

    return {"scanned": len(pairs), "written": written, "skipped": skipped}


# ---------------------------------------------------------------------------
# Codex (OpenAI Codex CLI) session-log ingest
# ---------------------------------------------------------------------------


def parse_codex_jsonl(path: str) -> list[dict]:
    """Parse a Codex CLI rollout JSONL into a list of turn-pair dicts.

    Codex writes rollouts at ``~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl``.
    Each line is one of ``session_meta``, ``event_msg``, ``turn_context``,
    or ``response_item``. Conversation content lives under ``response_item``
    with ``payload.type == "message"``.

    Pairing semantics:

    - Drop ``role=developer`` records (system permissions / instructions).
    - Collapse *consecutive* same-role message records into one segment —
      streamed assistant output across multiple records becomes one
      ``assistant_output``; auto-injected env context + user prompt
      becomes one ``user_input``.
    - Emit one turn pair per (user-segment → assistant-segment) transition.
      A trailing user segment with no following assistant is dropped.

    Source uuid is deterministic: ``codex:<session_id>:turn-<index>``
    from ``session_meta.id`` and the zero-based turn index. Re-parsing
    yields identical uuids, so re-ingest is a no-op.

    Returns dicts with the same shape as :func:`parse_claude_code_jsonl`:
    ``user_input``, ``assistant_output``, ``model``, ``user_uuid``,
    ``assistant_uuid``, ``timestamp``, ``session_id``, ``cwd``,
    ``git_branch`` (empty for codex), ``version``.

    Non-JSON lines, ``payload.type != "message"`` records, and unmatched
    segments are tolerated and dropped without raising.
    """
    import json as _json

    session_id = ""
    cwd = ""
    version = ""
    messages: list[dict] = []

    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = _json.loads(line)
            except _json.JSONDecodeError:
                continue
            rec_type = rec.get("type")
            payload = rec.get("payload") or {}
            if rec_type == "session_meta":
                session_id = payload.get("id") or ""
                cwd = payload.get("cwd") or ""
                version = payload.get("cli_version") or ""
            elif rec_type == "response_item" and payload.get("type") == "message":
                role = payload.get("role")
                if role in ("user", "assistant"):
                    messages.append({
                        "role": role,
                        "content": payload.get("content") or [],
                        "timestamp": rec.get("timestamp") or "",
                    })

    if not messages:
        return []

    # Collapse consecutive same-role records into segments.
    segments: list[dict] = []
    for m in messages:
        if segments and segments[-1]["role"] == m["role"]:
            segments[-1]["content"].extend(m["content"])
        else:
            segments.append({
                "role": m["role"],
                "content": list(m["content"]),
                "timestamp": m["timestamp"],
            })

    # Pair (user-segment, assistant-segment).
    pairs: list[dict] = []
    i = 0
    turn_index = 0
    while i < len(segments) - 1:
        seg = segments[i]
        next_seg = segments[i + 1]
        if seg["role"] == "user" and next_seg["role"] == "assistant":
            pairs.append({
                "user_input": _codex_segment_text(seg["content"]),
                "assistant_output": _codex_segment_text(next_seg["content"]),
                "model": "",  # codex session_meta has no model name field
                "user_uuid": f"codex:{session_id}:turn-{turn_index}",
                "assistant_uuid": f"codex:{session_id}:turn-{turn_index}-asst",
                "timestamp": seg["timestamp"],
                "session_id": session_id,
                "cwd": cwd,
                "git_branch": "",
                "version": version,
            })
            turn_index += 1
            i += 2
        else:
            i += 1

    return pairs


def _codex_segment_text(content_blocks: list) -> str:
    """Concatenate text from codex content blocks (``input_text`` / ``output_text``)."""
    parts = []
    for block in content_blocks:
        if not isinstance(block, dict):
            continue
        if block.get("type") in ("input_text", "output_text"):
            t = block.get("text") or ""
            if t:
                parts.append(t)
    return "\n\n".join(parts)


def ingest_codex_jsonl(
    *,
    composition: CompositionService,
    path: str,
    principal_id: str,
    dry_run: bool = False,
    limit: int | None = None,
) -> dict:
    """Fold a Codex CLI rollout JSONL into the principal's ledger.

    Each turn pair becomes one episodic fragment via
    :func:`record_session_turn` with ``tool="codex"``. ``content.extra``
    carries the deterministic source uuid, session id, cwd, and codex
    CLI version for provenance.

    Idempotency mirrors :func:`ingest_claude_code_jsonl`: scans existing
    fragments for ``principal_id`` and skips any turn whose ``user_uuid``
    already appears as ``content.extra.source_uuid``. Re-running ingest
    on the same transcript is a no-op.

    Returns ``{"scanned": N, "written": M, "skipped": K}``. ``dry_run=True``
    leaves ``written`` at 0.
    """
    pairs = parse_codex_jsonl(path)
    if limit is not None:
        pairs = pairs[:limit]

    if dry_run:
        return {"scanned": len(pairs), "written": 0, "skipped": 0}

    seen_uuids = _existing_source_uuids(composition, principal_id)

    written = 0
    skipped = 0
    for pair in pairs:
        uuid = pair.get("user_uuid", "")
        if uuid and uuid in seen_uuids:
            skipped += 1
            continue
        record_session_turn(
            composition=composition,
            principal_id=principal_id,
            tool="codex",
            model=pair["model"] or None,
            user_input=pair["user_input"],
            assistant_output=pair["assistant_output"],
            event_time=pair["timestamp"] or None,
            extra={
                "source_uuid": uuid,
                "assistant_uuid": pair["assistant_uuid"],
                "session_id": pair["session_id"],
                "cwd": pair["cwd"],
                "version": pair["version"],
            },
        )
        if uuid:
            seen_uuids.add(uuid)
        written += 1

    return {"scanned": len(pairs), "written": written, "skipped": skipped}


# ---------------------------------------------------------------------------
# Per-tool parser dispatch — pluggable session-log ingest
# ---------------------------------------------------------------------------


def _ingest_claude_code(
    *,
    composition: CompositionService,
    path: str,
    principal_id: str,
    dry_run: bool,
    limit: int | None,
) -> dict:
    return ingest_claude_code_jsonl(
        composition=composition,
        path=path,
        principal_id=principal_id,
        dry_run=dry_run,
        limit=limit,
    )


def _ingest_codex(
    *,
    composition: CompositionService,
    path: str,
    principal_id: str,
    dry_run: bool,
    limit: int | None,
) -> dict:
    return ingest_codex_jsonl(
        composition=composition,
        path=path,
        principal_id=principal_id,
        dry_run=dry_run,
        limit=limit,
    )


def _ingest_stub(tool: str):
    """Build a stub parser that raises with a clear contributor pointer."""

    def _stub(**_kwargs) -> dict:
        raise NotImplementedError(
            f"Parser for tool='{tool}' is not yet implemented. To add it, "
            f"register a parser in axiom/memory/session_capture.py "
            f"(KNOWN_TOOL_PARSERS) and a turn-pair extractor matching "
            f"the {tool} session-log format. Until then, capture from "
            f"{tool} happens via the MCP append tool path (model-driven)."
        )

    return _stub


# Registry of known tools → ingest dispatcher.
# claude-code is canonical; the others are stubs that surface a clear
# pointer so contributors can add them incrementally without changing
# the ingest_session_log() surface.
KNOWN_TOOL_PARSERS: dict = {
    "claude-code": _ingest_claude_code,
    "codex": _ingest_codex,
    "opencode": _ingest_stub("opencode"),
    "gemini": _ingest_stub("gemini"),
    "chatgpt-desktop": _ingest_stub("chatgpt-desktop"),
}


def ingest_session_log(
    *,
    composition: CompositionService,
    path: str,
    principal_id: str,
    tool: str = "claude-code",
    dry_run: bool = False,
    limit: int | None = None,
) -> dict:
    """Dispatch to the right per-tool parser based on ``tool``.

    Raises :class:`ValueError` for unknown tool names; raises
    :class:`NotImplementedError` for tools whose parser hasn't been
    contributed yet (with a pointer to where to add it).
    """
    parser = KNOWN_TOOL_PARSERS.get(tool)
    if parser is None:
        known = ", ".join(sorted(KNOWN_TOOL_PARSERS))
        raise ValueError(
            f"unknown tool '{tool}' for ingest. Known: {known}"
        )
    return parser(
        composition=composition,
        path=path,
        principal_id=principal_id,
        dry_run=dry_run,
        limit=limit,
    )


def watch_ingest_claude_code_jsonl(
    *,
    composition: CompositionService,
    path: str,
    principal_id: str,
    interval_s: float = 5.0,
    max_iterations: int | None = None,
    sleep_fn=None,
) -> dict:
    """Polling-mode incremental ingest. Loops until ``max_iterations``.

    Each iteration calls :func:`ingest_claude_code_jsonl` on the path
    (idempotent — already-ingested turn pairs are skipped). Sleeps
    ``interval_s`` between iterations. Tolerates a missing path by
    treating each iteration as scanned=0 / written=0.

    For tests, ``max_iterations`` bounds the loop and ``sleep_fn``
    overrides ``time.sleep`` so the test can mutate the file between
    iterations deterministically. In production, both default to
    "run until interrupted" (max_iterations=None) using ``time.sleep``.

    Returns a cumulative report::

        {
            "iterations": int,
            "total_scanned": int,    # sum across iterations (turn pairs seen)
            "total_written": int,    # sum across iterations (new fragments)
            "total_skipped": int,    # sum across iterations (already in ledger)
            "path": str,
            "principal_id": str,
        }
    """
    import os as _os
    import time as _time

    sleep_fn = sleep_fn or _time.sleep

    iterations = 0
    total_scanned = 0
    total_written = 0
    total_skipped = 0

    try:
        while True:
            iterations += 1
            if _os.path.exists(path):
                report = ingest_claude_code_jsonl(
                    composition=composition,
                    path=path,
                    principal_id=principal_id,
                    dry_run=False,
                )
                total_scanned += report["scanned"]
                total_written += report["written"]
                total_skipped += report["skipped"]

            if max_iterations is not None and iterations >= max_iterations:
                break
            try:
                sleep_fn(interval_s)
            except KeyboardInterrupt:
                break
    except KeyboardInterrupt:
        pass

    return {
        "iterations": iterations,
        "total_scanned": total_scanned,
        "total_written": total_written,
        "total_skipped": total_skipped,
        "path": path,
        "principal_id": principal_id,
    }


def _existing_source_uuids(
    composition: CompositionService, principal_id: str,
) -> set[str]:
    """Build a set of source_uuids already in the ledger for this principal.

    O(N) scan over the principal's fragments. Sufficient for MVP; a
    proper source-uuid index lives in the projection / query layer
    (Stage 2 of ADR-033).
    """
    seen: set[str] = set()
    for artifact in composition.artifact_registry.list(kind="fragment"):
        data = artifact.data or {}
        provenance = data.get("provenance") or {}
        if provenance.get("principal_id") != principal_id:
            continue
        content = data.get("content") or {}
        extra = content.get("extra") or {}
        uuid = extra.get("source_uuid")
        if uuid:
            seen.add(uuid)
    return seen
