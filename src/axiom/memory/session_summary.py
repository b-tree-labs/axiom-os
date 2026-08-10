# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Long-term session memory — populate PromptComposer's session_memory layer.

Every session writes one or more episodic MemoryFragments describing
what happened. Over time these accumulate into a per-principal memory
of prior interactions. This module is the bridge that surfaces that
memory back into the prompt at the next turn.

Three layers:

- :func:`list_fragments_by_principal` — raw retrieval by owner, ordered
  newest-first, with a configurable cap.
- :func:`build_session_memory_summary` — renders the N most recent
  session events as a compact text block suitable for prompt
  injection.
- :func:`inject_session_memory` — adds the summary as a
  ``session_memory`` contribution on an existing
  :class:`PromptComposer`, marked ``required=False`` so it's the
  first thing compaction drops under token pressure.

v0.1: pure retrieval + plain-text concatenation. An LLM-summarized
rollup over many fragments lives in a later phase and can plug in
behind the same :func:`build_session_memory_summary` signature
without caller changes.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from axiom.infra.prompt_composer import PromptComposer
    from axiom.memory.composition import CompositionService
    from axiom.memory.fragment import MemoryFragment


# Fragment content ``fact_kind``s that represent user-visible session
# events — i.e. things worth reminding the agent about at the next turn.
# Audit-style fragments (retrieval_audit, federated_inference_request)
# are filtered out: they're for offline analysis, not prompt context.
SESSION_EVENT_KINDS: frozenset[str] = frozenset(
    {
        "session_event",
        "chat_turn",
        "classroom_session",
        "quiz_submission",
    }
)


# ---------------------------------------------------------------------------
# Retrieval
# ---------------------------------------------------------------------------


def list_fragments_by_principal(
    composition: CompositionService,
    principal_id: str,
    *,
    limit: int = 20,
) -> list[MemoryFragment]:
    """Return the principal's episodic fragments, newest-first.

    Implemented by scanning the artifact registry directly since the
    :class:`CompositionService` read API requires explicit fragment
    ids. A by-owner index would be cleaner but is out-of-scope here;
    the scan is cheap for a single principal's history.
    """
    from axiom.memory.fragment import CognitiveType, fragment_from_dict

    matches: list[MemoryFragment] = []
    for artifact in composition.artifact_registry.list(kind="fragment"):
        data = artifact.data or {}
        provenance = data.get("provenance") or {}
        if provenance.get("principal_id") != principal_id:
            continue
        try:
            frag = fragment_from_dict(data)
        except Exception:
            continue
        if frag.cognitive_type is not CognitiveType.EPISODIC:
            continue
        matches.append(frag)

    # Sort newest-first by provenance timestamp (ISO-8601 lexicographic
    # comparison is correct for UTC timestamps).
    matches.sort(
        key=lambda f: f.provenance.timestamp, reverse=True,
    )
    return matches[:limit]


# ---------------------------------------------------------------------------
# Summary rendering
# ---------------------------------------------------------------------------


def build_session_memory_summary(
    composition: CompositionService,
    principal_id: str,
    *,
    max_fragments: int = 10,
) -> str:
    """Build a plain-text summary of prior sessions for this principal.

    Returns an empty string when the principal has no session
    history — callers check for truthiness before injecting into
    PromptComposer.

    The format is deliberately verbose enough to read naturally but
    compact enough to not blow the budget. Future iteration may add
    LLM-based rollup summaries; the signature stays stable.
    """
    fragments = list_fragments_by_principal(
        composition, principal_id, limit=max_fragments,
    )

    # Filter to session-relevant fragments (skip audit-only kinds).
    relevant = [
        f for f in fragments
        if f.content.get("fact_kind") in SESSION_EVENT_KINDS
        or f.content.get("summary")  # untyped but-summary-bearing frags
    ]
    if not relevant:
        return ""

    lines = [f"Prior sessions for {principal_id}:"]
    for frag in relevant:
        summary = frag.content.get("summary", "").strip()
        if not summary:
            continue
        when = frag.provenance.timestamp[:10]  # YYYY-MM-DD
        lines.append(f"  - [{when}] {summary}")
    if len(lines) == 1:
        # Header but no body — no real summaries to surface.
        return ""
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# PromptComposer injection
# ---------------------------------------------------------------------------


def inject_session_memory(
    composer: PromptComposer,
    composition: CompositionService,
    *,
    principal_id: str,
    max_fragments: int = 10,
    contribution_name: str = "session_history",
    source: str = "axiom.memory.session_summary",
) -> str | None:
    """Populate PromptComposer's ``session_memory`` layer from history.

    Returns the summary text that was added, or ``None`` if the
    principal has no history (the composer is left untouched in that
    case).

    The contribution is ``required=False`` so
    :meth:`PromptComposer.compact_to_budget` drops it first under
    token pressure. Stale context goes before current context.
    """
    summary = build_session_memory_summary(
        composition, principal_id, max_fragments=max_fragments,
    )
    if not summary:
        return None

    composer.add(
        layer="session_memory",
        name=contribution_name,
        content=summary,
        source=source,
        required=False,
    )
    return summary
