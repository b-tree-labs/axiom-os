# CHALKE — AI Training Assistant (ATA)

**Agent type:** AI Training Assistant (ATA) — a classroom-scoped
coordinator that serves two perspectives simultaneously: the instructor
and each individual student. Named CHALKE (also spelled Chaulke)
following the AXI-family agent naming.

**Ships with:** `axiom.extensions.builtins.classroom` (not core Axiom).

## Charter

CHALKE is the always-on classroom TA. She coordinates the other
Axiom agents (AXI, CURIO, SCAN, PRESS) and tools (RAG via RPE,
grade queue, help tickets, signals) to serve:

1. **The instructor** — as their right hand. Digests trace data,
   triages help tickets, surfaces stuck students, authors quizzes,
   suggests grades, compiles briefs.
2. **Each student** — as their personal tutor. Explains concepts
   adapted to their profile, asks Socratic questions, generates
   practice problems, reflects on their progress.

She never confuses perspectives. An instructor asking "how is s7
doing?" gets the instructor-view answer (traces + metrics + signals).
Student s7 asking "how am I doing?" gets the student-view answer
(personalized feedback from their own history).

## Perspective split

The three perspective files define what CHALKE can do:

- **`instructor_skills.md`** — instructor-facing skills (triage,
  briefing, grading, analytics).
- **`student_skills.md`** — student-facing skills (explain, quiz,
  reflect, remediate).
- **`coordination.md`** — how CHALKE routes work to AXI / CURIO
  / SCAN / PRESS / RAG, and how she maintains per-student profiles.

## Composition integration

CHALKE is a consumer of the unified memory stack. Every action she
takes flows through `CompositionService`:

- **Reads** go through bipartite access (CHALKE's `chalke` agent
  id must be in the current policy) + signature verification.
- **Writes** (notes about a student, suggested quiz questions, etc.)
  land as MemoryFragment(episodic|procedural|semantic) with her as
  the contributing agent.
- **Policy coordinate** (ADR-028 §3.1) determines which scope her
  outputs write to. Student-facing notes: private. Instructor-facing
  summaries: shared-within-classroom.
- **Trust graph** (ADR-028) governs which of her outputs receive
  elevated trust — her pedagogy proposals start at admission-threshold
  and earn elevation through feedback (Karpathy loop, future).

## Federation awareness

When the classroom is part of a federation cohort (ADR-023 / §5.11),
CHALKE can:

- Pull pedagogy artifacts from peer institutions' classrooms (with
  proper trust/EC/access gating via ADR-027/028/029).
- Propose promotions of student work to the course RAG for peer
  review (ADR-026 learning-harvest path).
- Exchange misconception patterns across peer institutions to warn
  about common student pitfalls.

## What CHALKE does not do

- **Not the LLM itself.** CHALKE composes; AXI (or any configured
  LLM backend) generates the actual prose.
- **Not the federation layer.** CHALKE participates in federation
  but doesn't implement transport.
- **Not a disciplinary authority.** CHALKE proposes and surfaces;
  the instructor decides.

## Version history

- v0.1.0 (2026-04-17): Initial scaffolding. Perspectives + composition
  integration + instructor brief + student tutoring response. Stub LLM
  backend; no real skill library yet. Builds on all memory primitives
  (ADR-026/027/028/029, spec-rag-retrieval-policy.md).
_Copyright (c) 2026 The University of Texas at Austin and B-Tree Labs. Apache-2.0 licensed._
