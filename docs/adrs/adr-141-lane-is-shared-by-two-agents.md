# ADR-141 — `lane` is shared by two agents, along the line ADR-046 already drew

**Status:** Accepted
**Date:** 2026-09-28
**Supersedes:** none
**Related:** ADR-046 (RIVET/TIDY boundary), ADR-031 (extension self-containment),
ADR-056 (CLI verbs over skills), ADR-139 (`caller_goal`)

## Context

`axi lane` gives each checkout an isolated database, a port pair and a record
of what it depends on. Two existing agents have an obvious claim on it, and
the obvious split — "git things to RIVET, cleanup to TIDY" — is wrong,
because ADR-046 already assigned *worktrees* to TIDY despite worktrees being
a git concept.

ADR-046's line is not about subject matter:

> **RIVET makes the green; TIDY removes the brown. They meet only at the
> event bus.**

RIVET is the authoritative signal of merge and ship state and performs **no
destructive git operations**. TIDY owns all destructive working-state
cleanup, under merge-confirmation, a guard stack, and reversibility.

## Decision

**Both agents, along ADR-046's existing line. `lane` itself stays
agent-free.**

1. **`lane` knows nothing about either agent.** No persona import, no
   reasoning dependency. It is a deterministic tool that happens to produce
   things two agents find useful. This is what keeps it liftable into another
   project, which was the reason for making it an extension.

2. **RIVET names.** `release/lane_signal.py` annotates a merge or release
   event with the lane its branch belonged to. Matching is **exact**: a
   near-match is worse than no match, because acting on the wrong lane is the
   failure. RIVET deletes nothing and decides nothing here.

3. **TIDY reclaims, by proposing.** `hygiene/lane_reclaim.py` says which
   lanes look finished and what could be done, and runs none of it. Whether a
   branch has landed is **injected**, not determined — that authority is
   RIVET's, and `None` ("cannot tell") blocks rather than proceeds.

4. **A new floor, F2, in TIDY's worktree assessment.** A worktree that a
   virtualenv holds an editable install into must not be pruned, whatever
   the staleness signals say.

## Consequences

- F2 exists because of a removal on 2026-09-28. `appkit-wt-cause` was
  landed, merged, clean and prunable — S2, S3 and S4 all fired correctly and
  the dirty floor was clear. Two long-running servers had been importing
  `axiom_appkit` from it for two days; nothing noticed until a stylesheet
  returned 500 and the traceback sat in an unread log. **Git cannot see a
  virtualenv binding**, so no git-derived signal could have prevented it.
  Run against the live workspace, F2 finds eleven bound checkouts — the
  person who did the removal had found two by hand.
- The proposal/act split is deliberate and asymmetric: a lane released by
  mistake is re-claimed in a second; a database dropped by mistake is
  somebody's afternoon. `lane release` prints `dropdb` and never runs it.
- A lane TIDY **cannot assess** is reported, not skipped. An early return on
  "no positive reason" silently dropped exactly the lanes it could not judge,
  and silence reads as "fine". Found by a test, not review.
- Neither agent is required. `axi lane` is fully usable with no LLM and no
  agent present, which is the floor `doctor` has to keep.
