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

from datetime import UTC, datetime
from pathlib import Path
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
    now = event_time or datetime.now(UTC).isoformat()
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
        when = when.replace(tzinfo=UTC)
    age = (datetime.now(UTC) - when).total_seconds()

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
    now = event_time or datetime.now(UTC).isoformat()
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


def parse_claude_code_jsonl(
    path: str, *, include_incomplete_tail: bool = False,
) -> list[dict]:
    """Parse a Claude Code session JSONL into a list of logical turns.

    One logical turn = one *real* user prompt plus every assistant
    message up to the next real user prompt. The tool-use loop in
    between (assistant ``tool_use`` → ``type=user`` line carrying a
    ``tool_result``) belongs to that turn; it is not a turn of its own.

    Returns dicts with keys: ``user_input``, ``assistant_output``,
    ``model``, ``tools_used``, ``user_uuid``, ``assistant_uuid``,
    ``timestamp``, ``session_id``, ``cwd``, ``git_branch``, ``version``.

    Non-conversation lines (permission-mode, file-history-snapshot,
    attachment, ai-title, queue-operation, system, last-prompt) are
    ignored, as is sidechain (subagent) traffic — that is the agent
    talking to itself, not the user's conversation.

    History: this used to pair each ``type=user`` record with the next
    ``type=assistant`` record and keep only ``type=="text"`` blocks. On
    agentic traffic that produced one fragment per tool hop with *both*
    sides empty — 79% of the ledger by the 2026-08-18 audit. Aggregating
    to the logical turn is the fix.

    The trailing turn is withheld while it is still **in flight** — a
    prompt whose assistant side has produced no prose yet. A scheduled
    sweep samples transcripts at arbitrary moments, so it routinely
    catches a turn mid-answer; writing that as finished is destructive,
    because the ``source_uuid`` dedupe then locks it and the answer
    landing seconds later can never be added. Holding it back costs one
    sweep interval and nothing else. Pass ``include_incomplete_tail`` for
    an archival dump that wants unanswered prompts too.

    No idempotency here — the caller decides whether to re-ingest.
    """
    import json as _json

    records: list[dict] = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = _json.loads(line)
            except _json.JSONDecodeError:
                continue
            if rec.get("type") not in ("user", "assistant"):
                continue
            # Subagent traffic is not the user's conversation.
            if rec.get("isSidechain") or rec.get("isMeta"):
                continue
            records.append(rec)

    turns: list[dict] = []
    open_turn: dict | None = None

    for rec in records:
        if rec.get("type") == "user":
            text = _extract_text_blocks(rec.get("message") or {})
            if not _is_real_user_prompt(rec):
                # tool_result / harness echo — stays inside the open turn.
                continue
            if open_turn is not None:
                turns.append(_finalize_turn(open_turn))
            open_turn = {
                "user_input": text,
                "assistant_chunks": [],
                "tools_used": [],
                "model": "",
                "assistant_uuid": "",
                "user_uuid": rec.get("uuid", ""),
                "timestamp": rec.get("timestamp", ""),
                "session_id": rec.get("sessionId", ""),
                "cwd": rec.get("cwd", ""),
                "git_branch": rec.get("gitBranch", ""),
                "version": rec.get("version", ""),
            }
        else:  # assistant
            if open_turn is None:
                continue
            msg = rec.get("message") or {}
            chunk = _extract_text_blocks(msg)
            if chunk.strip():
                open_turn["assistant_chunks"].append(chunk)
            open_turn["tools_used"].extend(_extract_tool_names(msg))
            open_turn["model"] = msg.get("model") or open_turn["model"]
            open_turn["assistant_uuid"] = rec.get("uuid", "") or open_turn[
                "assistant_uuid"
            ]

    if open_turn is not None:
        tail = _finalize_turn(open_turn)
        if include_incomplete_tail or tail["assistant_output"].strip():
            turns.append(tail)

    # A turn with nothing on either side is exactly the bug this parser
    # was rewritten to stop producing — never emit one.
    return [
        t for t in turns
        if t["user_input"].strip() or t["assistant_output"].strip()
    ]


def _extract_text_blocks(message: dict) -> str:
    """Pull human-readable prose out of a message's content."""
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n\n".join(
            block.get("text", "")
            for block in content
            if isinstance(block, dict) and block.get("type") == "text"
        )
    return ""


def _extract_tool_names(message: dict) -> list[str]:
    """Tool names invoked in this assistant message (provenance, not content)."""
    content = message.get("content")
    if not isinstance(content, list):
        return []
    return [
        block.get("name", "")
        for block in content
        if isinstance(block, dict)
        and block.get("type") == "tool_use"
        and block.get("name")
    ]


def _is_real_user_prompt(rec: dict) -> bool:
    """True when a ``type=user`` record is the human speaking.

    Claude Code delivers tool results on ``type=user`` lines. Those carry
    a ``tool_result`` block and no prose; treating them as user turns is
    what let tool output masquerade as things the user said.
    """
    content = (rec.get("message") or {}).get("content")
    if isinstance(content, str):
        return bool(content.strip())
    if isinstance(content, list):
        has_tool_result = any(
            isinstance(b, dict) and b.get("type") == "tool_result"
            for b in content
        )
        if has_tool_result:
            return False
        return bool(_extract_text_blocks(rec.get("message") or {}).strip())
    return False


def _finalize_turn(open_turn: dict) -> dict:
    """Collapse an accumulating turn into the flat turn dict callers expect."""
    turn = dict(open_turn)
    turn["assistant_output"] = "\n\n".join(turn.pop("assistant_chunks"))
    # Preserve call order, drop repeats.
    turn["tools_used"] = list(dict.fromkeys(turn["tools_used"]))
    return turn


def ingest_claude_code_jsonl(
    *,
    composition: CompositionService,
    path: str,
    principal_id: str,
    dry_run: bool = False,
    limit: int | None = None,
    salience_gate: bool = True,
) -> dict:
    """Fold a Claude Code session JSONL into the principal's ledger.

    Each turn pair becomes one episodic fragment via ``record_session_turn``.
    The originating tool is ``claude-code`` and ``content.extra`` carries
    the source uuid + session id + cwd + git branch for provenance.

    Idempotency: scans existing fragments for principal_id and skips any
    turn whose ``user_uuid`` already appears in the ledger as
    ``content.extra.source_uuid`` **with real content attached**. A
    hollow fragment (both sides empty) does not suppress re-capture —
    treating it as "already captured" is what made the 2026-08-18
    empties permanent. Re-running ingest on the same transcript is
    otherwise a no-op; a transcript that grew in place writes only its
    new turns.

    Salience: turns that carry nothing worth recovering later ("ok" →
    "Done.") are counted under ``filtered`` and not written. Pass
    ``salience_gate=False`` for a lossless dump.

    Returns ``{"scanned": N, "written": M, "skipped": K, "filtered": F}``.
    ``dry_run=True`` leaves ``written`` at 0 (and counts everything as
    scanned, none skipped since nothing's evaluated against the ledger).
    """
    turns = parse_claude_code_jsonl(path)
    if limit is not None:
        turns = turns[:limit]

    return _write_turns(
        composition=composition,
        turns=turns,
        principal_id=principal_id,
        tool="claude-code",
        dry_run=dry_run,
        salience_gate=salience_gate,
    )


def _write_turns(
    *,
    composition: CompositionService,
    turns: list[dict],
    principal_id: str,
    tool: str,
    dry_run: bool = False,
    salience_gate: bool = True,
    extra_fields: dict[str, Any] | None = None,
) -> dict:
    """Shared write path for every transcript parser.

    Claude Code, Codex, and Cursor all land here so the salience gate,
    the hollow-aware dedupe, and the provenance shape stay identical no
    matter which surface the user was sitting in.
    """
    from axiom.memory.salience import score_turn, summarize_turn

    if dry_run:
        return {
            "scanned": len(turns), "written": 0, "skipped": 0, "filtered": 0,
        }

    seen_uuids = _existing_source_uuids(composition, principal_id)

    written = 0
    skipped = 0
    filtered = 0
    for turn in turns:
        uuid = turn.get("user_uuid", "")
        if uuid and uuid in seen_uuids:
            skipped += 1
            continue

        tools_used = turn.get("tools_used") or []
        if salience_gate:
            verdict = score_turn(
                user_input=turn["user_input"],
                assistant_output=turn["assistant_output"],
                tools_used=tools_used,
                prompt_only=(extra_fields or {}).get(
                    "capture_mode") == "prompt_only",
            )
            if not verdict.salient:
                filtered += 1
                continue

        extra = {
            "source_uuid": uuid,
            "assistant_uuid": turn.get("assistant_uuid", ""),
            "session_id": turn.get("session_id", ""),
            "cwd": turn.get("cwd", ""),
            "git_branch": turn.get("git_branch", ""),
            "version": turn.get("version", ""),
        }
        if tools_used:
            extra["tools_used"] = tools_used
        if extra_fields:
            extra.update(extra_fields)

        record_session_turn(
            composition=composition,
            principal_id=principal_id,
            tool=tool,
            model=turn.get("model") or None,
            user_input=turn["user_input"],
            assistant_output=turn["assistant_output"],
            summary=summarize_turn(
                user_input=turn["user_input"],
                assistant_output=turn["assistant_output"],
                tools_used=tools_used,
            ),
            event_time=turn.get("timestamp") or None,
            extra=extra,
        )
        if uuid:
            seen_uuids.add(uuid)  # protect against same-turn repeats in one ingest
        written += 1

    return {
        "scanned": len(turns),
        "written": written,
        "skipped": skipped,
        "filtered": filtered,
    }


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
    salience_gate: bool = True,
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

    return _write_turns(
        composition=composition,
        turns=pairs,
        principal_id=principal_id,
        tool="codex",
        dry_run=False,
        salience_gate=salience_gate,
    )


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


# ---------------------------------------------------------------------------
# Claude Code prompt-history backfill
# ---------------------------------------------------------------------------


def default_history_path(home: Path | None = None) -> Path:
    """Location of Claude Code's prompt history file."""
    base = Path(home) if home is not None else Path.home()
    return base / ".claude" / "history.jsonl"


def parse_claude_code_history(path: str) -> list[dict]:
    """Parse ``~/.claude/history.jsonl`` into prompt-only turn dicts.

    One record per prompt the user typed: ``display`` (the prompt),
    ``project`` (cwd), ``sessionId``, ``timestamp`` (epoch millis).

    This file outlives the session transcripts, which Claude Code deletes
    on a rolling ``cleanupPeriodDays`` window. Where the transcript is
    gone, this is the only surviving record of the work — the user's side
    of it, at least. Assistant output is genuinely unrecoverable, so
    these turns carry an empty ``assistant_output`` by construction and
    are stamped ``capture_mode="prompt_only"`` downstream.

    The source has no per-record uuid, so one is derived from the
    record's own content (session + timestamp + prompt). Deterministic,
    so re-running the backfill is a no-op rather than a duplicate.
    """
    import hashlib
    import json as _json

    turns: list[dict] = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = _json.loads(line)
            except _json.JSONDecodeError:
                continue
            display = (rec.get("display") or "").strip()
            if not display:
                continue
            prompt = display
            # `display` is what the user saw, which for a paste is only a
            # placeholder like "[Pasted text #1 +374 lines]". The body sits
            # in `pastedContents`; reading just `display` discarded 241,714
            # characters of real context across the live history file.
            # Older entries kept only a contentHash — nothing to recover
            # there, so the placeholder stands alone.
            pasted = rec.get("pastedContents")
            if isinstance(pasted, dict):
                bodies = [
                    entry["content"]
                    for _, entry in sorted(pasted.items())
                    if isinstance(entry, dict) and entry.get("content")
                ]
                if bodies:
                    prompt = prompt + "\n\n" + "\n\n".join(bodies)

            session_id = rec.get("sessionId") or ""
            raw_ts = rec.get("timestamp")
            event_time = ""
            if isinstance(raw_ts, (int, float)):
                seconds = raw_ts / 1000 if raw_ts > 1e11 else float(raw_ts)
                event_time = datetime.fromtimestamp(
                    seconds, tz=UTC,
                ).isoformat()

            # Key identity on the RECORD, not on the text we assembled from
            # it. Hashing the enriched prompt would mint a new id every time
            # we learn to read more of a record (as happened when
            # pastedContents was added), duplicating turns already captured.
            digest = hashlib.sha256(
                f"{session_id}\x00{raw_ts}\x00{display}".encode()
            ).hexdigest()[:32]

            turns.append({
                "user_input": prompt,
                "assistant_output": "",
                "model": "",
                "tools_used": [],
                "user_uuid": f"history:{digest}",
                "assistant_uuid": "",
                "timestamp": event_time,
                "session_id": session_id,
                "cwd": rec.get("project") or "",
                "git_branch": "",
                "version": "",
            })
    return turns


def ingest_claude_code_history(
    *,
    composition: CompositionService,
    path: str,
    principal_id: str,
    dry_run: bool = False,
    limit: int | None = None,
    salience_gate: bool = True,
    reset: bool = False,
) -> dict:
    """Backfill recoverable prompts from Claude Code's history file.

    Skips any prompt whose full exchange is already in the ledger — the
    retention windows overlap, and a half turn must never shadow a whole
    one we already captured from a transcript. That count comes back as
    ``already_captured``.
    """
    from axiom.memory.salience import score_turn

    turns = parse_claude_code_history(path)
    if limit is not None:
        turns = turns[:limit]

    purged = 0
    if reset and not dry_run:
        # Stable ids make re-ingest a no-op, which is right for routine runs
        # but wrong after the PARSER improves: the already-captured thin
        # version would win forever. Clearing the prompt-only set lets a
        # better read of the same source replace it. Only prompt_only
        # fragments go — they are all reconstructible from this file.
        purged = _purge_prompt_only(composition, principal_id)

    captured = _captured_prompts(composition, principal_id)

    if dry_run:
        # A dry run that reports zeros teaches the caller nothing. Do the
        # full classification, skip only the write.
        seen = _existing_source_uuids(composition, principal_id)
        already = filtered = would_write = skipped = 0
        for turn in turns:
            key = (turn["session_id"], " ".join(turn["user_input"].split()))
            if key in captured:
                already += 1
            elif turn["user_uuid"] in seen:
                skipped += 1
            elif salience_gate and not score_turn(
                user_input=turn["user_input"],
                assistant_output=turn["assistant_output"],
                prompt_only=True,
            ).salient:
                filtered += 1
            else:
                would_write += 1
        return {
            "scanned": len(turns), "written": would_write, "skipped": skipped,
            "filtered": filtered, "already_captured": already,
            "dry_run": True,
        }

    fresh = []
    already = 0
    for turn in turns:
        key = (turn["session_id"], " ".join(turn["user_input"].split()))
        if key in captured:
            already += 1
            continue
        fresh.append(turn)

    report = _write_turns(
        composition=composition,
        turns=fresh,
        principal_id=principal_id,
        tool="claude-code",
        dry_run=False,
        salience_gate=salience_gate,
        extra_fields={"capture_mode": "prompt_only",
                      "source": "claude-code-history"},
    )
    report["scanned"] = len(turns)
    report["already_captured"] = already
    report["purged"] = purged
    return report


def _purge_prompt_only(
    composition: CompositionService, principal_id: str,
) -> int:
    """Tombstone this principal's prompt-only fragments. Returns the count."""
    ids = []
    for artifact in composition.artifact_registry.list(kind="fragment"):
        data = artifact.data or {}
        provenance = data.get("provenance") or {}
        if provenance.get("principal_id") != principal_id:
            continue
        content = data.get("content") or {}
        if (content.get("extra") or {}).get("capture_mode") == "prompt_only":
            ids.append(artifact.id)
    for fragment_id in ids:
        composition.artifact_registry.delete(
            fragment_id, reason="prompt-only refresh: re-reading history source",
        )
    return len(ids)


def _captured_prompts(
    composition: CompositionService, principal_id: str,
) -> set[tuple[str, str]]:
    """(session_id, normalized prompt) for turns already captured in full.

    Whitespace-normalized because a transcript preserves the prompt as
    sent while the history file stores it as displayed.
    """
    seen: set[tuple[str, str]] = set()
    for artifact in composition.artifact_registry.list(kind="fragment"):
        data = artifact.data or {}
        provenance = data.get("provenance") or {}
        if provenance.get("principal_id") != principal_id:
            continue
        content = data.get("content") or {}
        if content.get("fact_kind") != "chat_turn":
            continue
        user_input = (content.get("user_input") or "").strip()
        if not user_input:
            continue
        extra = content.get("extra") or {}
        seen.add(
            (extra.get("session_id") or "", " ".join(user_input.split()))
        )
    return seen


def _ingest_claude_code_history(
    *,
    composition: CompositionService,
    path: str,
    principal_id: str,
    dry_run: bool,
    limit: int | None,
) -> dict:
    return ingest_claude_code_history(
        composition=composition,
        path=path,
        principal_id=principal_id,
        dry_run=dry_run,
        limit=limit,
    )


# ---------------------------------------------------------------------------
# Cursor session capture
# ---------------------------------------------------------------------------


#: Cursor bubble ``type`` discriminator.
_CURSOR_USER = 1
_CURSOR_ASSISTANT = 2


def default_cursor_store_paths(home: Path | None = None) -> list[Path]:
    """Candidate Cursor global-storage DBs for this machine.

    Cursor is a VS Code fork, so the store lives under the per-platform
    application-support directory. Returning candidates (rather than one
    path) keeps the sweep working across platforms and across the
    Cursor/Cursor-Nightly split without the caller knowing any of it.
    """
    base = Path(home) if home is not None else Path.home()
    rel = "User/globalStorage/state.vscdb"
    return [
        base / "Library/Application Support/Cursor" / rel,          # macOS
        base / "Library/Application Support/Cursor Nightly" / rel,
        base / ".config/Cursor" / rel,                              # Linux
        base / "AppData/Roaming/Cursor" / rel,                      # Windows
    ]


def parse_cursor_state(
    path: str, *, include_incomplete_tail: bool = False,
) -> list[dict]:
    """Parse a Cursor ``state.vscdb`` into a list of logical turns.

    Cursor stores chat in the ``cursorDiskKV`` table:

    - ``composerData:<composerId>`` — one conversation
    - ``bubbleId:<composerId>:<bubbleId>`` — one message, where ``type``
      1 = user and 2 = assistant, ``text`` holds the prose, ``createdAt``
      is ISO-8601, and ``modelInfo.modelName`` names the model the user
      had selected.

    Turns aggregate the same way as the Claude Code parser: one user
    bubble plus every assistant bubble up to the next user bubble.
    Agent/tool bubbles carry no ``text`` and never become fragments.

    The model is recorded verbatim — Cursor may be pointed at any
    vendor, and the ledger's job is to say which one produced the turn,
    not to assume.

    Read-only: Cursor owns this file and normally holds it open under
    WAL, so the DB is copied to a scratch file before reading rather
    than opened in place.
    """
    import json as _json

    rows = _read_cursor_kv(path)

    # composerId -> [bubble dicts]
    conversations: dict[str, list[dict]] = {}
    for key, value in rows:
        if not key.startswith("bubbleId:"):
            continue
        parts = key.split(":")
        if len(parts) < 3:
            continue
        composer_id = parts[1]
        try:
            bubble = _json.loads(value)
        except (_json.JSONDecodeError, TypeError):
            continue
        if not isinstance(bubble, dict):
            continue
        bubble.setdefault("bubbleId", parts[2])
        conversations.setdefault(composer_id, []).append(bubble)

    turns: list[dict] = []
    for composer_id, bubbles in conversations.items():
        bubbles.sort(key=lambda b: (b.get("createdAt") or "", b.get("bubbleId") or ""))
        turns.extend(_cursor_turns_for_conversation(
            composer_id, bubbles,
            include_incomplete_tail=include_incomplete_tail,
        ))

    turns.sort(key=lambda t: t["timestamp"] or "")
    return turns


def _read_cursor_kv(path: str) -> list[tuple[str, Any]]:
    """Return ``cursorDiskKV`` rows without mutating the source database.

    Copying is deliberate. Opening Cursor's live DB in place would create
    ``-wal``/``-shm`` sidecars next to a file another process owns; a
    memory backstop must never be able to corrupt the tool it observes.
    """
    import shutil
    import sqlite3
    import tempfile

    src = Path(path)
    with tempfile.TemporaryDirectory(prefix="axi-cursor-") as tmp:
        scratch = Path(tmp) / "state.vscdb"
        shutil.copyfile(src, scratch)
        # Cursor may have uncheckpointed pages in a sidecar WAL; copy it
        # too so recent turns are not silently invisible.
        for suffix in ("-wal", "-shm"):
            sidecar = Path(str(src) + suffix)
            if sidecar.exists():
                shutil.copyfile(sidecar, Path(str(scratch) + suffix))
        conn = sqlite3.connect(scratch)
        try:
            return list(
                conn.execute("SELECT key, value FROM cursorDiskKV")
            )
        except sqlite3.DatabaseError:
            return []
        finally:
            conn.close()


def _cursor_turns_for_conversation(
    composer_id: str,
    bubbles: list[dict],
    *,
    include_incomplete_tail: bool = False,
) -> list[dict]:
    """Aggregate one Cursor conversation's bubbles into logical turns.

    Cursor is captured by the scheduled sweep only — it has no at-turn
    hook — so it is the most exposed to being sampled mid-answer. The
    trailing in-flight turn is withheld for the same reason as the
    Claude Code parser.
    """
    turns: list[dict] = []
    open_turn: dict | None = None

    # Cursor stamps modelInfo on whichever bubble the picker was set on,
    # not on every bubble. Seed from the conversation so a turn is never
    # left anonymous just because the selection happened later in the
    # thread — which model produced a turn is half the value of capturing
    # it in a multi-model workflow.
    last_model = _cursor_conversation_model(bubbles)

    for bubble in bubbles:
        text = (bubble.get("text") or "").strip()
        model_info = bubble.get("modelInfo") or {}
        if isinstance(model_info, dict) and model_info.get("modelName"):
            last_model = model_info["modelName"]

        if bubble.get("type") == _CURSOR_USER:
            if not text:
                continue
            if open_turn is not None:
                turns.append(_finalize_turn(open_turn))
            open_turn = {
                "user_input": text,
                "assistant_chunks": [],
                "tools_used": _cursor_tool_names(bubble),
                "model": last_model,
                "assistant_uuid": "",
                "user_uuid": bubble.get("bubbleId", ""),
                "timestamp": bubble.get("createdAt", ""),
                "session_id": composer_id,
                "cwd": "",
                "git_branch": "",
                "version": "",
            }
        elif bubble.get("type") == _CURSOR_ASSISTANT and open_turn is not None:
            if text:
                open_turn["assistant_chunks"].append(text)
                open_turn["assistant_uuid"] = bubble.get("bubbleId", "")
            open_turn["tools_used"].extend(_cursor_tool_names(bubble))
            open_turn["model"] = open_turn["model"] or last_model

    if open_turn is not None:
        tail = _finalize_turn(open_turn)
        if include_incomplete_tail or tail["assistant_output"].strip():
            turns.append(tail)

    return [
        t for t in turns
        if t["user_input"].strip() or t["assistant_output"].strip()
    ]


def _cursor_conversation_model(bubbles: list[dict]) -> str:
    """First model named anywhere in a Cursor conversation, else empty."""
    for bubble in bubbles:
        model_info = bubble.get("modelInfo") or {}
        if isinstance(model_info, dict) and model_info.get("modelName"):
            return model_info["modelName"]
    return ""


def _cursor_tool_names(bubble: dict) -> list[str]:
    """Tool names referenced by a Cursor bubble (provenance, not content).

    Cursor records agent tool calls in ``toolFormerData`` — one call per
    bubble — and only rarely populates ``toolResults``. Reading just the
    latter made tool-heavy Cursor turns look like empty half-captures
    when the assistant had in fact done a great deal of work and simply
    written no prose.
    """
    names: list[str] = []
    results = bubble.get("toolResults")
    if isinstance(results, list):
        for entry in results:
            if isinstance(entry, dict) and entry.get("name"):
                names.append(entry["name"])
    former = bubble.get("toolFormerData")
    if isinstance(former, dict) and former.get("name"):
        names.append(former["name"])
    return names


def ingest_cursor_state(
    *,
    composition: CompositionService,
    path: str,
    principal_id: str,
    dry_run: bool = False,
    limit: int | None = None,
    salience_gate: bool = True,
) -> dict:
    """Fold a Cursor ``state.vscdb`` into the principal's ledger.

    Provenance is stamped ``tool="cursor"`` with the model Cursor was
    configured with, so a later recall can tell a Cursor/Grok turn from
    a Claude Code/Opus one while both live in the same ledger.
    """
    turns = parse_cursor_state(path)
    if limit is not None:
        turns = turns[:limit]

    return _write_turns(
        composition=composition,
        turns=turns,
        principal_id=principal_id,
        tool="cursor",
        dry_run=dry_run,
        salience_gate=salience_gate,
    )


def _ingest_cursor(
    *,
    composition: CompositionService,
    path: str,
    principal_id: str,
    dry_run: bool,
    limit: int | None,
) -> dict:
    return ingest_cursor_state(
        composition=composition,
        path=path,
        principal_id=principal_id,
        dry_run=dry_run,
        limit=limit,
    )


# Registry of known tools → ingest dispatcher.
# claude-code is canonical; the others are stubs that surface a clear
# pointer so contributors can add them incrementally without changing
# the ingest_session_log() surface.
KNOWN_TOOL_PARSERS: dict = {
    "claude-code": _ingest_claude_code,
    "codex": _ingest_codex,
    "cursor": _ingest_cursor,
    "claude-code-history": _ingest_claude_code_history,
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

    Only uuids whose fragment actually carries content count as seen. A
    hollow fragment (both sides empty) is a capture failure, not a
    capture — letting it suppress re-ingest is what made the 2026-08-18
    empties unrecoverable, since the real text could never land under a
    uuid the ledger had already claimed.

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
        if not uuid:
            continue
        if is_hollow_content(content):
            continue
        seen.add(uuid)
    return seen


def is_hollow_content(content: dict) -> bool:
    """True when a chat_turn fragment stored no conversation at all.

    The signature of the parser bug: a fragment with provenance,
    timestamps and session metadata, but neither side of the exchange.
    """
    if content.get("fact_kind") != "chat_turn":
        return False
    return not (
        (content.get("user_input") or "").strip()
        or (content.get("assistant_output") or "").strip()
    )
