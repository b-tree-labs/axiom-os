# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``memory.capture-sweep`` skill — one pass over every harness on disk.

The cross-tool promise is that the ledger fills the same way whichever
tool the user is sitting in. Honouring that needs a single scheduled
sweep that knows where each harness keeps its transcripts, rather than a
per-tool script that only ever runs when that tool is the one in use.

This skill also owns the success criterion the previous launchd script
got wrong. That script counted *files opened* and logged ``ok: ingested 4
file(s), 0 error(s)`` on every run — including four days of runs that
wrote nothing, because every turn deduped against a uuid already in the
ledger. Files touched is not a health metric. What matters is:

1. how many **fragments** were written, per tool, and
2. whether the ledger's newest conversation turn is still recent.

A sweep that writes nothing is fine on a quiet afternoon and alarming
after a silent week, so staleness — not write count — is what flips
``ok`` to false and gives a watchdog something real to alert on.

Absent harnesses are normal (not everyone runs every tool) and a single
corrupt transcript must never abort the pass, so per-source failures are
collected and reported rather than raised.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from axiom.infra.skills import SkillContext, SkillResult

_AGENT = "axi-memory"

#: Ledger older than this and we say so. One working day — long enough
#: to sit through an evening and a morning without crying wolf, short
#: enough that a silent week is impossible to miss.
DEFAULT_MAX_LEDGER_AGE_HOURS = 24

#: Only sweep transcripts touched recently; the rest are already folded
#: in and rescanning them costs an O(N) ledger scan apiece.
DEFAULT_LOOKBACK_DAYS = 2


def _recent_files(root: Path, pattern: str, lookback_days: int) -> list[Path]:
    """Transcripts under ``root`` modified within the lookback window.

    Subagent transcripts are excluded: that is the agent talking to
    itself, not the user's conversation.
    """
    if not root.exists():
        return []
    cutoff = (datetime.now(UTC) - timedelta(days=lookback_days)).timestamp()
    out: list[Path] = []
    for path in root.rglob(pattern):
        if "subagents" in path.parts:
            continue
        try:
            if path.stat().st_mtime >= cutoff:
                out.append(path)
        except OSError:
            continue
    return sorted(out)


def _newest_chat_turn_age_hours(composition: Any, principal: str) -> float | None:
    """Hours since the newest real conversation turn, or None if there is none.

    Heartbeats are deliberately excluded. A heartbeat proves the process
    ran; only a chat_turn proves capture actually worked — conflating
    the two is how a dead capture path kept reporting green.
    """
    newest: str | None = None
    for artifact in composition.artifact_registry.list(kind="fragment"):
        data = artifact.data or {}
        provenance = data.get("provenance") or {}
        if provenance.get("principal_id") != principal:
            continue
        content = data.get("content") or {}
        if content.get("fact_kind") != "chat_turn":
            continue
        stamp = content.get("event_time") or provenance.get("timestamp") or ""
        if stamp and (newest is None or stamp > newest):
            newest = stamp
    if not newest:
        return None
    try:
        when = datetime.fromisoformat(newest.replace("Z", "+00:00"))
    except ValueError:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return (datetime.now(UTC) - when).total_seconds() / 3600.0


def capture_sweep(params: dict[str, Any], ctx: SkillContext | None) -> SkillResult:
    """Sweep every known harness into the principal's ledger.

    Params:

    - ``composition`` (required) — the principal's CompositionService.
    - ``principal`` (required) — ledger owner.
    - ``claude_projects`` — Claude Code project root (default
      ``~/.claude/projects``).
    - ``codex_sessions`` — Codex rollout root (default ``~/.codex/sessions``).
    - ``cursor_paths`` — explicit Cursor ``state.vscdb`` paths; defaults
      to the per-platform candidates that exist on this machine.
    - ``lookback_days`` — transcript mtime window (default 2).
    - ``max_ledger_age_hours`` — staleness alarm threshold (default 24).
    - ``salience_gate`` — set False for a lossless dump.
    """
    from axiom.memory.session_capture import (
        default_cursor_store_paths,
        ingest_claude_code_jsonl,
        ingest_codex_jsonl,
        ingest_cursor_state,
    )

    composition = params.get("composition")
    if composition is None:
        return SkillResult(ok=False, errors=["no composition service provided"])
    principal = params.get("principal")
    if not principal:
        return SkillResult(ok=False, errors=["--principal is required"])

    # Explicit None checks, not `or` — 0 is a meaningful value for both of
    # these (a zero-hour threshold is how you assert "must be fresh right
    # now" in a test or a tight monitor) and falsy-coalescing would
    # silently substitute the default.
    lookback_param = params.get("lookback_days")
    lookback = int(
        DEFAULT_LOOKBACK_DAYS if lookback_param is None else lookback_param
    )
    max_age_param = params.get("max_ledger_age_hours")
    max_age = float(
        DEFAULT_MAX_LEDGER_AGE_HOURS if max_age_param is None else max_age_param
    )
    salience_gate = params.get("salience_gate", True)

    home = Path.home()
    claude_root = Path(
        params.get("claude_projects") or (home / ".claude" / "projects")
    )
    codex_root = Path(
        params.get("codex_sessions") or (home / ".codex" / "sessions")
    )
    if params.get("cursor_paths") is not None:
        cursor_paths = [Path(p) for p in params["cursor_paths"]]
    else:
        cursor_paths = [p for p in default_cursor_store_paths() if p.exists()]

    written_by_tool: dict[str, int] = {}
    filtered_by_tool: dict[str, int] = {}
    files_scanned = 0
    problems: list[str] = []

    sources: list[tuple[str, Path, Any]] = []
    for path in _recent_files(claude_root, "*.jsonl", lookback):
        sources.append(("claude-code", path, ingest_claude_code_jsonl))
    for path in _recent_files(codex_root, "*.jsonl", lookback):
        sources.append(("codex", path, ingest_codex_jsonl))
    for path in cursor_paths:
        if path.exists():
            sources.append(("cursor", path, ingest_cursor_state))

    for tool, path, ingest in sources:
        files_scanned += 1
        try:
            report = ingest(
                composition=composition,
                path=str(path),
                principal_id=principal,
                salience_gate=salience_gate,
            )
        except Exception as exc:  # one bad transcript never aborts the pass
            problems.append(f"{tool}:{path.name}: {exc}")
            continue
        written_by_tool[tool] = written_by_tool.get(tool, 0) + report["written"]
        filtered_by_tool[tool] = (
            filtered_by_tool.get(tool, 0) + report.get("filtered", 0)
        )

    written = sum(written_by_tool.values())
    age_hours = _newest_chat_turn_age_hours(composition, principal)

    errors: list[str] = []
    if age_hours is None:
        errors.append(
            f"ledger has no chat_turn fragments for {principal} — capture "
            "has never succeeded on this machine"
        )
    elif age_hours > max_age:
        errors.append(
            f"ledger is stale: newest captured turn is {age_hours:.1f}h old "
            f"(threshold {max_age:.0f}h) — capture is not reaching the ledger"
        )

    value = {
        "principal": principal,
        "written": written,
        "written_by_tool": written_by_tool,
        "filtered_by_tool": filtered_by_tool,
        "files_scanned": files_scanned,
        "sources": len(sources),
        "ledger_age_hours": age_hours,
        "max_ledger_age_hours": max_age,
        "problems": problems,
    }
    actions = (
        [f"captured {written} fragment(s) across {len(written_by_tool)} tool(s)"]
        if written else []
    )
    return SkillResult(
        ok=not errors,
        value=value,
        errors=errors + problems,
        actions_taken=actions,
    )
