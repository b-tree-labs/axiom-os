# CHALKE — Instructor-Facing Skills

When CHALKE serves the instructor perspective, these are the skills
she offers.

## Signal digest

**`chalke.for_instructor(classroom_id).daily_brief()`** produces the
3-5 most important things the instructor needs to know today:

- Stuck students (from SCAN's `student_stuck` signals)
- Misconceptions detected across the cohort
- Engagement anomalies (low/high engagement)
- Objective gaps (coverage below threshold)
- Help tickets requiring instructor attention

Compiled from `classroom_signals.record_signal` fragments through
the composition read path. Respects access control — only signals
the instructor is authorized to see are included.

## Triage

**`triage_help_queue()`** orders open + in-progress help tickets by
priority:

- High: misconception_detected signal on the same student, previous
  resolution low-quality, instructor explicitly flagged.
- Medium: unresolved > 24h, multiple tickets from same student.
- Low: straightforward procedural questions that CHALKE auto-resolves
  and annotates as "resolved by chalke suggestion; instructor
  review optional."

## Grading support

**`grade_suggestions(assessment_id)`** produces a queue of free-text
responses with:

- LLM-suggested score + rationale (via the LLMGrader wired into
  `quiz_scoring.auto_score`).
- Comparable-response samples (how similar answers scored in this
  course's history).
- Rubric-clause citations (which clause each score element references).

Instructor reviews → overrides → `quiz_scoring.override_score` writes
the final grade as MemoryFragment(semantic).

## Analytics

**`cohort_analytics()`** surfaces aggregate metrics from
`classroom.metrics` — per-student turn count, RAG hit rate, label
distribution, topic coverage, session-type breakdown — filtered to
the cohort the instructor has access to.

**`side_by_side_compare(student_ids, question_id)`** lists how each
named student answered the same question — a capability Claude cannot
offer (no cohort context).

## Quiz authoring

**`propose_quiz(topic, difficulty, count=5)`** generates candidate
quiz questions grounded in the course corpus via RPE (intent="generative",
top_k=10). Instructor reviews, edits, promotes to the assessment.

## Course-level proposals

**`propose_curriculum_update(reason)`** surfaces evidence-backed
proposals to adjust course materials — for example when multiple
students hit the same wall, or when a pack-version update in a peer
institution's course claims higher outcomes. The instructor approves
or rejects; approved proposals become course_lifecycle.republish_with_bump
events (ADR-026).

## Federation-aware

When the classroom is in a multi-node cohort, CHALKE can:

- `peer_misconception_alerts()` — pulls misconception patterns
  detected at peer institutions.
- `peer_promotion_proposals()` — reviews outstanding proposals from
  peer classrooms to promote student work to the course RAG.
- `cross_org_cohort_compare()` — compares this cohort's metrics
  against peer cohorts (with consent).
_Copyright (c) 2026 The University of Texas at Austin and B-Tree Labs. Apache-2.0 licensed._
