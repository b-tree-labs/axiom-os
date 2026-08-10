# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Grade-explain: one-click provenance trace of what informed a score (#26).

An adoption-differentiator feature — no commercial LLM product can
produce this because it requires the full composition stack (fragments,
ownership, audit log) that our v1 architecture provides.

Given a student_id + assessment_id + question_id, grade_explain walks
the composition data to produce:

- The scored response (ScoredResponse materialized as
  MemoryFragment(semantic) via #72)
- The original student trace turn (if a chat preceded the scoring —
  MemoryFragment(episodic) via #71)
- Any override events (from the audit log — who re-graded, when, why)
- Any breach flags (post_filter audit entries per #75)
- The rubric trace, if a RubricScore was attached

Output is a plain dict ready to render as JSON, markdown, or UI.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from axiom.memory.composition import CompositionService


@dataclass
class GradeExplanation:
    """Complete provenance bundle for one student's answer to one question."""

    student_id: str
    assessment_id: str
    question_id: str
    score_fragment: dict | None = None      # the ScoredResponse fragment
    response_trace: dict | None = None      # the chat/quiz trace fragment
    override_events: list[dict] = field(default_factory=list)
    breach_events: list[dict] = field(default_factory=list)
    audit_entries: list[dict] = field(default_factory=list)


def explain_grade(
    composition: CompositionService,
    student_id: str,
    assessment_id: str,
    question_id: str,
) -> GradeExplanation:
    """Walk the composition data and assemble a grade explanation.

    Works against the SQLite-backed ArtifactRegistry + AuditLog:
    - Find the ScoredResponse fragment for this (student, assessment, question).
    - Find any episodic fragments (traces) whose content references the
      same (assessment_id, question_id).
    - Pull audit-log entries that reference the score fragment's id.
    - Identify override events (reviewed_by set after initial write).
    """
    result = GradeExplanation(
        student_id=student_id,
        assessment_id=assessment_id,
        question_id=question_id,
    )

    # 1. Score fragment (ScoredResponse materialized via #72)
    score_candidates = []
    trace_candidates = []
    for artifact in composition.artifact_registry.list(kind="fragment"):
        data = artifact.data
        content = data.get("content", {})
        if (
            content.get("student_id") == student_id
            and content.get("assessment_id") == assessment_id
            and content.get("question_id") == question_id
        ):
            ct = data.get("cognitive_type")
            if ct == "semantic":
                score_candidates.append((artifact.created_at, data))
            elif ct == "episodic" and content.get("session_type") == "quiz":
                trace_candidates.append((artifact.created_at, data))

    # Latest score wins (override supersedure)
    if score_candidates:
        score_candidates.sort(key=lambda p: p[0])
        result.score_fragment = score_candidates[-1][1]

    # Earliest trace shows the original answer moment
    if trace_candidates:
        trace_candidates.sort(key=lambda p: p[0])
        result.response_trace = trace_candidates[0][1]

    # 2. Audit entries referencing this score fragment
    if result.score_fragment:
        score_id = result.score_fragment.get("id")
        for entry in composition.audit_log.read_all():
            if entry.get("fragment_id") == score_id:
                result.audit_entries.append(entry)
                if entry.get("entry_type") == "post_filter_breach":
                    result.breach_events.append(entry)

    # 3. Override events: each later-than-initial write by a different
    #    reviewer is an override. For v0 we surface the reviewed_by
    #    field directly from the score fragment content.
    if result.score_fragment:
        content = result.score_fragment.get("content", {})
        if content.get("reviewed_by"):
            result.override_events.append({
                "reviewed_by": content.get("reviewed_by"),
                "reviewed_at": content.get("reviewed_at"),
                "review_note": content.get("review_note"),
                "final_score": content.get("final_score"),
                "prior_score": content.get("suggested_score")
                    or content.get("auto_score"),
            })

    return result


def render_markdown(explanation: GradeExplanation) -> str:
    """Render an explanation as a markdown document for instructor review."""
    lines = [
        f"# Grade explanation — {explanation.student_id} / "
        f"{explanation.assessment_id} / {explanation.question_id}",
        "",
    ]

    if explanation.score_fragment:
        c = explanation.score_fragment.get("content", {})
        lines.extend([
            "## Final score",
            f"- **{c.get('final_score', 'pending')}** ({c.get('question_type')})",
            f"- Auto-score: {c.get('auto_score')}",
            f"- LLM-suggested: {c.get('suggested_score')}",
            f"- Rationale: {c.get('rationale', '—')}",
            "",
        ])
    else:
        lines.extend(["## Final score", "_No score recorded yet._", ""])

    if explanation.response_trace:
        tc = explanation.response_trace.get("content", {})
        lines.extend([
            "## Original response",
            f"- Recorded at: {tc.get('event_time')}",
            f"- Response: {tc.get('response', '—')}",
            "",
        ])

    if explanation.override_events:
        lines.append("## Override events")
        for ov in explanation.override_events:
            lines.append(
                f"- **{ov.get('reviewed_by')}** @ {ov.get('reviewed_at')}: "
                f"{ov.get('prior_score')} → {ov.get('final_score')}"
                + (f" — note: {ov['review_note']}" if ov.get("review_note") else "")
            )
        lines.append("")

    if explanation.breach_events:
        lines.append("## Post-filter breach events")
        for b in explanation.breach_events:
            lines.append(f"- {b.get('outcome', 'unknown')}: {b.get('timestamp')}")
        lines.append("")

    lines.append(f"## Audit trail ({len(explanation.audit_entries)} entries)")
    for e in explanation.audit_entries[-10:]:  # last 10 for brevity
        lines.append(
            f"- `{e.get('entry_type')}` by {e.get('principal_id')} "
            f"via {e.get('agent_id')} @ {e.get('timestamp')}"
        )

    return "\n".join(lines)
