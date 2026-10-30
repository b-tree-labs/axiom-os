# ADR-176: Emergent work is a first-class flow — detected, classified, and decided, never silently absorbed

**Status:** Accepted (2026-10-06)
**Related:** [ADR-161](adr-161-program-tracking-is-a-composed-clerk.md) (the coordinator that runs the flow), [ADR-162](adr-162-the-watcher-primitive.md) (the capture feeders that detect the work), [ADR-165](adr-165-program-self-update-and-per-consumer-change-detection.md) (the change log the decision is recorded in), [ADR-166](adr-166-program-membership-is-a-principal-not-a-tracker-seat.md) (the actor an emergent item is attributed to), [ADR-167](adr-167-the-program-is-editable-through-its-own-tool.md) (the mutation verbs that adopt an item), [ADR-171](adr-171-adaptivity-every-external-system-is-an-optional-additive-connector.md) (reuse existing parts — no new engine), [ADR-172](adr-172-products-are-the-composable-spine-up-a-ladder-to-a-north-star.md) (the product spine and the North Star the alignment axis reads), [ADR-173](adr-173-estimation-is-deterministic-the-model-proposes-a-size-never-a-date.md) (the shaping flow the decision is made in), `docs/prds/prd-program.md` (R20, R23, R24, R25), `docs/specs/spec-program.md` (§Forward design)

## Context

The program models planned work: an item is proposed, committed, and then
delivered, flowing from the plan toward evidence. But the plan is never the
whole of what happens. People do work that was never planned — a fix noticed in
passing, a spike that turned into a feature, a detour that became the most
valuable thing shipped that week. The capture feeders (ADR-162) already observe
all of it: every commit, merge request, and signed record crosses the feeder
whether a tracked item predicted it or not.

Until now that work had two fates, both wrong. Either it was **silently
absorbed** — attributed to the nearest item or to none, so the plan quietly
described work it never asked for and no one could tell plan from drift — or it
was **silently dropped**, falling off the record because nothing tracked
pointed at it, so a serendipitous discovery left no trace and a distraction left
no warning. The existing `untracked_issue` finding (ADR-162) caught one shape of
this at the tracker-issue level, and `unowned_item` / orphan-contributor
findings caught others, but there was no first-class treatment of *work that was
done and maps to no tracked item* — the level above orphan contributors.

A program needs to see this flow precisely because it runs **opposite in
direction** to the plan: planned work flows from intention to delivery; emergent
work flows from delivery back toward intention, arriving as a fact that the plan
must now account for. Two risks force the decision. First, **drift invisible**:
untracked divergence accumulates until the program's real trajectory and its
stated one have nothing to do with each other, and no one decided that. Second,
**serendipity lost**: the best unplanned work is penalized by friction — if
adopting a discovery into the plan is expensive, people stop surfacing it, and
the program learns nothing from its own luck.

## Decision

**Emergent work — work that was done but never planned — is a first-class flow,
opposite in direction to planned work. The program detects it, classifies it by
alignment, routes it to an explicit human decision, logs that decision with its
rationale, and makes the balance of discovery against drift visible. It is never
silently absorbed into the plan nor silently dropped.** The flow is five steps,
each riding machinery that already exists:

1. **Detect.** The capture feeders already observe all landed work. Work that
   maps to no tracked item is **emergent** and lands in an **inbox** — an
   append-only set of detected-but-unmapped work items, each attributed to its
   actor (ADR-166). This is orphan detection at the *work* level, one level above
   the orphan-contributor and orphan-issue findings. It is never rounded into an
   existing item and never let fall off the record.

2. **Classify.** Each emergent item is positioned on a single **alignment
   axis**: does it ladder up to an existing product and, through it, toward the
   North Star (ADR-172) — a likely improvement — or does it not — a candidate
   side-trail? The tracker **proposes** the reading from what it can see (the
   actor, the lane, the artifacts the work touched, the products those map to);
   it **does not decide**. The proposal is a classification, not a verdict.

3. **Decide and log.** A human, in the conversational-shaping flow (R20,
   ADR-173), resolves each emergent item: **adopt** it (create a tracked item and
   attach it to an existing product, or to a **new** product — capturing
   serendipity), **name it a distraction** (stop the work, or park it for later),
   or **defer** it (decide later; it stays in the inbox). Adoption, distraction,
   and deferral all run through the existing mutation verbs (ADR-167) and are
   recorded in the change log (ADR-165) with the human's **rationale** — so the
   log is an audit of *why the program changed course*, not just that it did.

4. **Visualize.** A **divergence view** plots emergent work against the plan by
   alignment — work advancing toward the North Star versus work pulling away from
   it — over time, so the program's **discovery-versus-drift balance** is visible
   at a glance rather than inferred after the fact.

5. **Principle — strictness comes from visibility, not prohibition.** Nothing is
   forbidden: a side-trail is a legitimate thing to choose. But no side-trail
   stays invisible, every divergence surfaces for an explicit decision, and the
   tracker may **gently flag** when an actor accumulates divergent untracked work
   (a prompt to decide, never a block). Serendipity is protected by making
   **adoption cheap** — one step from discovery to a tracked product. The program
   **surfaces, classifies, decides, and logs; it never silently absorbs and never
   blocks.**

The commitment this ADR makes is narrow and testable: emergent work is
**surfaced and classified**, and **adopt / name-distraction / defer is a logged
human decision**. Everything else reuses parts already decided — the capture
feeders (ADR-162) detect, the accountability ladder (ADR-172) supplies the
alignment axis, the shaping flow (R20) is where the human decides, the mutation
verbs (ADR-167) enact, and the change log (ADR-165) records. It is **not a new
engine** (ADR-171's anti-monster posture): the inbox, the alignment classifier,
and the divergence view are new *questions* asked of existing data, not new
subsystems.

## Options considered

- **Silently absorb emergent work into the nearest tracked item.** Zero friction,
  and the status quo's first failure mode. The plan ends up describing work it
  never asked for, plan and drift become indistinguishable, and the record lies
  about intention. Rejected — it defeats the point of having a plan.
- **Silently drop work that maps to no tracked item.** The status quo's second
  failure mode. A discovery leaves no trace and a distraction raises no flag; the
  program cannot learn from its own luck or catch its own drift. Rejected.
- **Forbid untracked work — refuse to recognize any landed work without a prior
  item.** Makes the plan authoritative by fiat. But work happens whether the plan
  predicted it or not, and a tracker that refuses to see it is simply blind in a
  politer way; worse, it penalizes exactly the serendipitous discovery a program
  most wants to keep. Rejected: strictness through prohibition drives the work
  off the record, the opposite of the goal.
- **Auto-classify and auto-adopt by a confidence score.** Let the alignment
  classifier decide, adopting high-confidence improvements and dropping low ones
  without a human. Rejected: it reintroduces silent absorption (and silent
  dropping) through the back door, and the alignment call — improvement or
  distraction — is precisely the judgement a program head must own, not delegate
  to a score. The classifier proposes; the human decides.
- **A standalone divergence tracker, separate from the program tool.** A
  dedicated subsystem for emergent work. Rejected as a new engine (ADR-171):
  detection is the existing feeders, the decision is the existing shaping flow,
  the record is the existing change log. A separate system would duplicate all of
  it and split the one audit trail in two.

## Consequences

- The program data model gains an **emergent-work inbox** (detected-but-unmapped
  work items, each with a proposed alignment classification and, once resolved, a
  logged decision referencing the change log) — additive, reversible, carried
  losslessly like every other block (§Forward design). Adoption of an emergent
  item is an ordinary mutation (ADR-167), so it writes through the one commit path
  and advances the snapshot exactly like a hand-created item.
- The change log gains the record of **why the program changed course**: an
  adopt / distraction / defer decision is logged with its actor (`by`) and its
  rationale, so a reader can reconstruct not only that an item appeared but the
  discovery or the detour that produced it. The change-kind vocabulary extends
  deliberately and reversibly (ADR-165) to carry these.
- The **divergence view** is one more render over the inbox and the product graph
  — no external call, no new store (ADR-171). It makes a previously invisible
  property (discovery vs drift, over time) a first-class surface.
- **Serendipity becomes cheap to keep.** Adoption is one step from an inbox item
  to a tracked product (existing or new), so the path of least resistance is to
  surface and decide, not to hide. This is the behavioral point of the whole flow.
- What this commits us to: emergent work is a permanent, opposite-direction
  counterpart to planned work, and the inbox-plus-decision shape is now part of the
  program's contract. Removing it would return the two silent-failure modes. The
  gentle accumulation flag is a prompt, never a gate — raising it to a hard block
  would violate the visibility-not-prohibition principle and is explicitly out of
  scope.
- Cost: every emergent item is a decision someone must eventually make. That is
  deliberate — the deferral path exists so the decision can wait, but the item
  does not leave the inbox until a human resolves it, which keeps the backlog of
  undecided divergence honest rather than letting it evaporate.
