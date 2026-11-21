# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Side-by-side student answer comparison (#25).

"How did 5 students answer question Q3?" — a capability Claude
literally cannot provide (no cohort context). Reads from the
composition stack: quiz fragments (via #72) and episodic traces
(via #71) are queried per-student and assembled for instructor
review.

The output surface is deliberately minimal (a dataclass + markdown
renderer) so this module is reusable from CLI, MCP server, VS Code
extension, or a future instructor dashboard.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from axiom.memory.composition import CompositionService


@dataclass
class AnswerRow:
    """One student's answer to the comparison question."""

    student_id: str
    answer: str | None = None
    final_score: float | None = None
    question_type: str | None = None
    reviewed_by: str | None = None
    trace_timestamp: str | None = None


@dataclass
class AnswerComparison:
    """Full side-by-side view for a question across a cohort."""

    assessment_id: str
    question_id: str
    rows: list[AnswerRow] = field(default_factory=list)

    @property
    def score_spread(self) -> float | None:
        """Range of final_scores across the cohort. None if <2 scored."""
        scored = [r.final_score for r in self.rows if r.final_score is not None]
        if len(scored) < 2:
            return None
        return max(scored) - min(scored)


def compare_answers(
    composition: CompositionService,
    *,
    assessment_id: str,
    question_id: str,
    student_ids: list[str],
) -> AnswerComparison:
    """Assemble per-student answer rows from the composition stack.

    For each student:
    - Pull latest semantic (scored) fragment matching the question.
    - Pull earliest episodic (trace) fragment for that question as the
      original answer text.
    - Merge into an AnswerRow.
    """
    result = AnswerComparison(
        assessment_id=assessment_id, question_id=question_id,
    )

    # Index fragments by (student_id, cognitive_type) for fast lookup.
    # Student id may live in content (quiz score fragments) or in
    # provenance.principal_id (episodic trace fragments). Fall back.
    by_student: dict[str, dict[str, list[tuple[float, dict]]]] = {}
    for artifact in composition.artifact_registry.list(kind="fragment"):
        data = artifact.data
        content = data.get("content", {})
        if (
            content.get("assessment_id") != assessment_id
            or content.get("question_id") != question_id
        ):
            continue
        sid = content.get("student_id") or data.get("provenance", {}).get(
            "principal_id"
        )
        if sid not in student_ids:
            continue
        ct = data.get("cognitive_type")
        by_student.setdefault(sid, {}).setdefault(ct, []).append(
            (artifact.created_at, data)
        )

    for sid in student_ids:
        entries = by_student.get(sid, {})
        row = AnswerRow(student_id=sid)

        # Latest semantic = scored fragment
        semantic = entries.get("semantic", [])
        if semantic:
            semantic.sort(key=lambda p: p[0])
            score_data = semantic[-1][1]
            sc = score_data.get("content", {})
            row.final_score = sc.get("final_score")
            row.question_type = sc.get("question_type")
            row.reviewed_by = sc.get("reviewed_by")

        # Earliest episodic = original answer
        episodic = entries.get("episodic", [])
        if episodic:
            episodic.sort(key=lambda p: p[0])
            trace_data = episodic[0][1]
            tc = trace_data.get("content", {})
            row.answer = tc.get("response")
            row.trace_timestamp = tc.get("event_time")

        result.rows.append(row)

    return result


def render_markdown(comparison: AnswerComparison) -> str:
    """Render side-by-side comparison as markdown for instructor review."""
    lines = [
        f"# Answer comparison — {comparison.assessment_id} / "
        f"{comparison.question_id}",
        "",
    ]
    if comparison.score_spread is not None:
        lines.append(f"**Score spread:** {comparison.score_spread:.2f}")
        lines.append("")

    lines.append("| Student | Score | Answer | Reviewer |")
    lines.append("|---|---|---|---|")
    for row in comparison.rows:
        answer = (row.answer or "—")[:80].replace("|", "\\|").replace("\n", " ")
        score = f"{row.final_score}" if row.final_score is not None else "—"
        reviewer = row.reviewed_by or "—"
        lines.append(f"| {row.student_id} | {score} | {answer} | {reviewer} |")
    return "\n".join(lines)
