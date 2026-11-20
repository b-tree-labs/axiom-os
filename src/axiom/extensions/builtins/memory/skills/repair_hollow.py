# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``memory.repair-hollow`` skill — purge content-free chat_turn fragments.

A hollow fragment carries provenance, a timestamp and session metadata
but neither side of the conversation. They are the residue of the
pair-wise transcript parser, which treated each hop of a tool-use loop
(assistant ``tool_use`` → harness ``tool_result``) as a turn and kept
only ``type=="text"`` blocks, leaving both sides empty. The 2026-08-18
audit found them to be 79% of the ledger.

They are worse than useless. Because the ingest dedupe keyed on
``content.extra.source_uuid``, every hollow shell permanently claimed
its turn's uuid — so the real text could never be written, no matter how
many times the transcript was re-ingested. Purging them releases those
uuids and lets a re-sweep recover the actual conversation.

Deliberately narrow: it only removes fragments that are *provably*
empty, and ``dry_run`` is the way to look before leaping.
"""

from __future__ import annotations

from typing import Any

from axiom.infra.skills import SkillContext, SkillResult

_AGENT = "axi-memory"
_REASON = "hollow chat_turn fragment (no user_input, no assistant_output)"


def repair_hollow(params: dict[str, Any], ctx: SkillContext | None) -> SkillResult:
    """Find and remove hollow chat_turn fragments for a principal.

    Params:

    - ``composition`` (required) — the principal's CompositionService.
    - ``principal`` (required) — ledger owner.
    - ``include_partial`` — also purge turns captured mid-answer (a
      prompt with an empty assistant side), but ONLY where the session
      transcript still exists to re-capture from.
    - ``claude_projects`` — transcript root used for that recoverability
      check (default ``~/.claude/projects``).
    - ``dry_run`` — report what would go, change nothing.
    """
    from axiom.memory.session_capture import is_hollow_content

    composition = params.get("composition")
    if composition is None:
        return SkillResult(ok=False, errors=["no composition service provided"])
    principal = params.get("principal")
    if not principal:
        return SkillResult(ok=False, errors=["--principal is required"])
    dry_run = bool(params.get("dry_run"))

    include_partial = bool(params.get("include_partial"))
    recoverable = (
        _sessions_with_transcripts(params.get("claude_projects"))
        if include_partial else set()
    )

    hollow_ids: list[str] = []
    released_uuids: list[str] = []
    partial_ids: list[str] = []
    partial_unrecoverable = 0
    kept = 0

    for artifact in composition.artifact_registry.list(kind="fragment"):
        data = artifact.data or {}
        provenance = data.get("provenance") or {}
        if provenance.get("principal_id") != principal:
            continue
        content = data.get("content") or {}
        if content.get("fact_kind") != "chat_turn":
            continue
        if is_hollow_content(content):
            hollow_ids.append(artifact.id)
            uuid = (content.get("extra") or {}).get("source_uuid")
            if uuid:
                released_uuids.append(uuid)
        elif include_partial and _is_partial_capture(content):
            # Only purge what we can actually get back. Dropping a
            # partial whose transcript is gone would trade a half turn
            # for nothing at all.
            session = (content.get("extra") or {}).get("session_id") or ""
            if session in recoverable:
                partial_ids.append(artifact.id)
            else:
                partial_unrecoverable += 1
                kept += 1
        else:
            kept += 1

    if dry_run:
        return SkillResult(
            ok=True,
            value={
                "would_purge": len(hollow_ids) + len(partial_ids),
                "would_purge_hollow": len(hollow_ids),
                "would_purge_partial": len(partial_ids),
                "partial_unrecoverable": partial_unrecoverable,
                "kept": kept,
                "released_source_uuids": len(released_uuids),
                "dry_run": True,
            },
        )

    purged = 0
    failures: list[str] = []
    for fragment_id in hollow_ids + partial_ids:
        try:
            composition.artifact_registry.delete(fragment_id, reason=_REASON)
            purged += 1
        except Exception as exc:
            failures.append(f"{fragment_id}: {exc}")

    return SkillResult(
        ok=not failures,
        value={
            "purged": purged,
            "purged_hollow": len(hollow_ids),
            "purged_partial": len(partial_ids),
            "partial_unrecoverable": partial_unrecoverable,
            "kept": kept,
            "released_source_uuids": len(released_uuids),
        },
        errors=failures,
        actions_taken=(
            [f"purged {purged} hollow fragment(s); "
             f"{len(released_uuids)} source uuid(s) freed for re-capture"]
            if purged else []
        ),
    )


def _is_partial_capture(content: dict) -> bool:
    """True when a turn was captured mid-answer and then locked.

    The sweep samples transcripts at arbitrary moments, so it can read a
    prompt whose response has not been written yet. Backfilled prompts
    look identical in shape but are intentional and unrecoverable, so
    they are excluded by ``capture_mode``.
    """
    if (content.get("extra") or {}).get("capture_mode") == "prompt_only":
        return False
    return bool(
        (content.get("user_input") or "").strip()
        and not (content.get("assistant_output") or "").strip()
    )


def _sessions_with_transcripts(claude_projects: str | None) -> set[str]:
    """Session ids whose transcript is still on disk, so re-capture works."""
    from pathlib import Path

    root = Path(claude_projects) if claude_projects else (
        Path.home() / ".claude" / "projects"
    )
    if not root.exists():
        return set()
    return {
        path.stem for path in root.rglob("*.jsonl")
        if "subagents" not in path.parts
    }
