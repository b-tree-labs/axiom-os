# CHALKE — Student-Facing Skills

When CHALKE serves a specific student's perspective, these are the
skills she offers.

## Personalized explanation

**`chalke.for_student(student_id).explain(topic)`** answers a topic
question tuned to the student's profile:

- Language + locale (per `international` platform layer — future)
- Current knowledge level (derived from their quiz + trace history)
- Pedagogy preference (Socratic vs didactic; learned from prior
  interactions)
- Their recent confusion patterns (from SCAN signals)

Uses RPE intent=`teaching` → maturity_floor=Frameworks →
pedagogy_weight=0.7 → citations grounded in the course corpus.

## Socratic questioning

**`socratic_prompt(concept)`** asks the student a leading question
designed to surface their current mental model. Useful when CHALKE
detects they have a misconception but wants them to realize it
themselves.

## Adaptive examples

**`example_for(concept, difficulty)`** generates a worked example
calibrated to the student's demonstrated level. Pulls from
course corpus first (authoritative) and only generates new examples
when no suitable corpus example exists.

## Metacognitive reflection

**`metacognitive_review()`** runs RPE intent=`metacognitive` →
strategy=`trace` → queries the student's own trace history. Produces
a reflective summary: what they've covered, where they've struggled,
what to focus on next. Pulls from their MemoryFragment(episodic)
trace records.

## Practice problem generation

**`practice(topic, count=3)`** generates practice problems with
solutions, scaled to the student's current level. Available when
RPE intent=`generative` + course assessment templates.

## Exam pacing

**`exam_prep_plan(quiz_id, days_until)`** produces a day-by-day
prep plan for a scheduled quiz: topics to review, recommended
practice, expected time investment per day.

## Re-teaching prerequisites

**`check_prerequisites(topic)`** identifies concept dependencies the
student might be weak on (from their quiz history) and offers to
re-teach them before proceeding with the requested topic. Avoids the
"I understand X but not the Y it rests on" trap.

## Research loop (CURIO routing)

When the student's query fits the research intent (multi-hop,
breadth-preferred), CHALKE routes to CURIO via `coordination.md`
protocols. CURIO runs the iterative research loop; CHALKE
summarizes outputs back to the student.

## Help escalation

**`/help <issue>`** creates a help ticket (`help_tickets.create_help_ticket`
routed through the composition stack). Student sees "your question
is queued with your TA" — the instructor sees the structured ticket
with context turns.

## Privacy posture

Student-facing CHALKE respects:

- Per-student memory scope — this student's traces, not the cohort.
- The student's own access graph — can't see peer students' records
  unless the instructor grants visibility.
- Consent flags — research-export opt-out, cross-org sharing opt-out.

Every read is audit-logged; every write is signed + scoped.
_Copyright (c) 2026 The University of Texas at Austin and B-Tree Labs. Apache-2.0 licensed._
