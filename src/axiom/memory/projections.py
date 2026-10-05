# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Layer 3 projections — pure functions over (events, graph, task).

ADR-033 Layer 3 first inhabitant. A projection is a pure function:

    project(task: TaskSpec, store, graph) -> typed result

Same ``(events, graph, task)`` always yields the same result, so
projections are cacheable + replayable + auditable. Extension-specific
projections (briefs, study plans, retrieval contexts) compose with
shared primitives like ``RecentActivityProjection`` rather than
re-implementing event walks per extension.

This module ships the first shared primitive — episodic recency for
a single principal in a single scope — and is the foundation for the
"episodic memory in ask context" win identified in the classroom
end-to-end review. Extensions consume it as a library; production
wiring threads ``ArtifactRegistry`` in directly. There is no
projection cache yet — Stage 3 of ADR-033 adds one when measurement
shows it pays off.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from axiom.artifacts.registry import ArtifactRegistry
from axiom.memory.fragment import (
    CognitiveType,
    MemoryFragment,
    fragment_from_dict,
)

# ---------------------------------------------------------------------------
# TaskSpec — generic descriptor extensions subclass for typed parameters
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TaskSpec:
    """Generic projection task descriptor.

    Per ADR-033 Layer 3: extensions subclass this for typed
    parameters specific to their projection (StudyPlanTaskSpec,
    BriefTaskSpec, etc.). The base type is enough for the
    shared-primitive projections below.
    """

    task_type: str
    scope: str
    parameters: dict[str, Any] = field(default_factory=dict)
    as_of: str | None = None  # time-travel point; None = "now"


# ---------------------------------------------------------------------------
# RecentActivityProjection — first shared primitive
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RecentActivity:
    """Result type for ``RecentActivityProjection``."""

    scope: str
    principal_id: str
    fragments: list[MemoryFragment]

    @property
    def is_empty(self) -> bool:
        return not self.fragments


@dataclass
class RecentActivityProjection:
    """Project the most-recent N fragments for a principal in a scope.

    The "episodic memory in ask context" use case: when a student
    asks a question, fold their recent N interactions into the LLM
    system prompt so the model has continuity across sessions. This
    is the cheapest user-visible win called out in the classroom
    end-to-end review (`docs/working/classroom-memory-end-to-end-review-2026-04-25.md`).

    Generic by design: any extension that has principal-scoped
    fragments in L1 can use this. Classroom uses it for student
    asks; chat will use it for working memory; research loops can
    use it for "what has this investigator done lately."

    Filtering rules:

    - Only ``EPISODIC`` fragments are considered (this projection is
      about *what happened*, not *what is true*). Other cognitive
      types pass through extension-specific projections.
    - Scope is matched via ``content["classroom_id"]`` for the
      classroom case and via ``content["scope"]`` as a fallback.
      A future iteration will move scope to a top-level fragment
      field; today the bespoke-store dual-write path puts it in
      content.
    - Principal is matched via ``provenance.principal_id``.
    - Fragments are sorted by their semantic event-time descending
      and truncated to ``window_n``. Episodic fragments carry
      ``content["event_time"]`` per the EPISODIC validator; that's
      the canonical happened-at timestamp. Provenance.timestamp is
      the recorded-at timestamp (for backfilled or migrated events
      these can differ); we sort by happened-at because that's what
      "recent" means to the consumer.
    - ``as_of`` (when set on the TaskSpec) excludes fragments whose
      event_time is newer than the cutoff, enabling time-travel
      projections.

    Read-only — never mutates state.
    """

    artifact_registry: ArtifactRegistry
    window_n: int = 5

    def project(
        self, task: TaskSpec, *, principal_id: str,
    ) -> RecentActivity:
        """Compute the projection for ``(task.scope, principal_id)``.

        Returns a ``RecentActivity`` with up to ``window_n`` fragments
        sorted newest-first. Scope must come from the task; principal
        is a separate parameter so the same task can be re-projected
        per student in batch contexts (briefs, etc.).

        Fast path: when the registry's backend exposes ``find_fragments``
        (SQLiteBackend in v0), pushes the cognitive_type + principal_id
        + scope filter into a JSON1 SELECT — O(K) where K is the matching
        subset rather than O(N) over the full registry. Slow path is the
        Python-iterate fallback that any backend supports.
        """
        target_scope = task.scope
        as_of = task.as_of

        # Stage 3 fast path — push filter into SQL when available.
        backend = getattr(self.artifact_registry, "_backend", None)
        find_fragments = getattr(backend, "find_fragments", None)
        if find_fragments is not None:
            return self._fast_path(
                find_fragments=find_fragments,
                principal_id=principal_id,
                target_scope=target_scope,
                as_of=as_of,
            )

        return self._slow_path(
            principal_id=principal_id,
            target_scope=target_scope,
            as_of=as_of,
        )

    def _fast_path(
        self, *, find_fragments, principal_id: str, target_scope: str,
        as_of: str | None,
    ) -> RecentActivity:
        # SQL-side filter is restricted to the two domain-agnostic
        # dimensions covered by the SQLite expression index:
        # cognitive_type and principal_id. Scope filtering happens
        # post-hoc in Python with the both-keys lookup (``scope`` is the
        # recommended generic content key; ``classroom_id`` and other
        # extension-specific keys are accepted for backward compat with
        # adapters that haven't migrated yet).
        #
        # Without scope in the SQL WHERE we may over-fetch when one
        # principal participates in multiple scopes; we offset that with
        # a generous SQL limit and Python truncation. At Edge / 100k
        # fragments the cost stays well within the spec-memory targets.
        sql_limit = max(self.window_n * 8, 64)
        artifacts = find_fragments(
            cognitive_type="episodic",
            principal_id=principal_id,
            order_by_event_time_desc=True,
            limit=sql_limit,
        )
        fragments: list[MemoryFragment] = []
        for artifact in artifacts:
            try:
                frag = fragment_from_dict(artifact.data)
            except (KeyError, ValueError, TypeError):
                continue
            frag_scope = (
                frag.content.get("scope")
                or frag.content.get("classroom_id")
            )
            if frag_scope != target_scope:
                continue
            if as_of is not None:
                event_time = frag.content.get(
                    "event_time", frag.provenance.timestamp,
                )
                if event_time > as_of:
                    continue
            fragments.append(frag)
            if len(fragments) >= self.window_n:
                break
        return RecentActivity(
            scope=target_scope,
            principal_id=principal_id,
            fragments=fragments,
        )

    def _slow_path(
        self, *, principal_id: str, target_scope: str,
        as_of: str | None,
    ) -> RecentActivity:
        candidates: list[MemoryFragment] = []
        for artifact in self.artifact_registry.list(kind="fragment"):
            try:
                frag = fragment_from_dict(artifact.data)
            except (KeyError, ValueError, TypeError):
                continue
            if frag.cognitive_type is not CognitiveType.EPISODIC:
                continue
            if frag.provenance.principal_id != principal_id:
                continue
            frag_scope = (
                frag.content.get("classroom_id")
                or frag.content.get("scope")
            )
            if frag_scope != target_scope:
                continue
            event_time = frag.content.get("event_time", frag.provenance.timestamp)
            if as_of is not None and event_time > as_of:
                continue
            candidates.append(frag)

        candidates.sort(
            key=lambda f: f.content.get("event_time", f.provenance.timestamp),
            reverse=True,
        )
        return RecentActivity(
            scope=target_scope,
            principal_id=principal_id,
            fragments=candidates[: self.window_n],
        )


def format_recent_for_prompt(activity: RecentActivity) -> str:
    """Render a ``RecentActivity`` projection as plain-text prompt context.

    Convention: a short header naming the scope + principal, then one
    line per fragment in newest-first order. Designed to be appended
    to a system prompt as a section the model treats as working
    memory.

    Returns empty string when activity has no fragments — caller can
    skip the section entirely rather than render an empty header.
    """
    if activity.is_empty:
        return ""

    lines = [
        f"Recent activity for {activity.principal_id} in {activity.scope}:",
    ]
    for frag in activity.fragments:
        ts = frag.content.get("event_time", frag.provenance.timestamp)
        question = frag.content.get("question", "(no question text)")
        mode = frag.content.get("mode", "ask")
        had_answer = frag.content.get("had_answer", False)
        marker = "✓" if had_answer else "·"
        lines.append(f"  [{ts}] {marker} ({mode}) {question}")
    return "\n".join(lines)


__all__ = [
    "RecentActivity",
    "RecentActivityProjection",
    "TaskSpec",
    "format_recent_for_prompt",
]
