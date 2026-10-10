# Spec — the case construct

**Status:** Living. Amend in the PR that changes the behaviour.
**Owns:** the vocabulary, operations and invariants of the oversight
surface. Implementations live in `axiom/extensions/builtins/receipts/`
and the evaluator half in `.../fleet/status.py`.
**Related:** ADR-126 (typed decision receipts), spec-receipts-surface
(the surface that renders this), PRD R13/R14/R19.

---

## 1. Why this document exists

The pieces below were derived in flight over one working session, each
in response to a concrete defect or a founder note. They are now
coherent enough to state, and stating them is what stops the next six
features each re-deciding the same questions.

Most of the machinery is borrowed, and the sources are named in §6
because reading them is cheaper than rediscovering what they learned.
One piece has no clean analog in computing, and §7 says which.

---

## 2. Vocabulary

Each term below is one thing, with one definition, used in exactly that
sense everywhere including in code.

**Claim.** One typed assertion about one entity at one moment:
`(entity, claim_kind, status, evidence, observed_at, derivation)`.
`status` is drawn from a fixed five-value taxonomy and nothing else:

| status | means |
|---|---|
| `green` | checked, and it held |
| `unproven` | asserted without evidence that could be checked |
| `stale` | nothing recent arrived |
| `failed` | evidence contradicts the assertion |
| `unknown` | nothing has said |

`unproven` is never `failed` and `unknown` is never `green`. Collapsing
either distinction is the single most common way an oversight surface
starts lying, because both collapses are locally convenient and globally
false.

**Condition.** A `(claim_kind, status)` pair — the KIND of trouble,
independent of which entity has it. "A heartbeat that has gone stale" is
a condition; "node-a's heartbeat has gone stale" is a claim. Conditions
are the key of the one table that supplies a headline, a detail, a fix
and a remedy (`receipts.conditions`), and they are what `calibrate`
groups by (§3), because "how good is this solver's judgement" is only
answerable per kind of trouble.

Named here because §3 already keyed an operation on it while §2 did not
define it, which is how a load-bearing term becomes folklore.

**Case.** The decision unit: the set of non-green claims about ONE
entity within one site, with an identity that survives re-derivation.
`case_id = "c-" + sha1(site|entity_kind|entity_id)[:8]`. A node with
three bad claims is one situation, not three.

**Derivation.** How a claim was reached: the stored `inputs`, the `rule`
applied, a `verify` command that checks it *without this platform*, and
the `limit` — what the claim cannot establish. Data, not a view.

**Cause.** A partial order over a case's claims: which claim explains
which, by declared rule only. Never inferred.

**Reach.** What a case touches, counted from declared edges only, with
the sources it was counted from. A computed zero says "0 declared
dependents"; it never says "unknown".

**Handling.** Whether a case needs a person, and which single fact
stopped it being handled automatically. The reasons are a closed set
(`held`, `no_fix`, `not_ours`, `tried`, `did_not_work`, `never_worked`)
and each is a checkable fact, never a tuned threshold.

**Offer.** What may be done about a case: the label, what it will do,
what it will NOT do, whether it records, and what it will run.

**Decision.** A typed choice: the `options` that existed, the one
`chosen`, the `decider`, the `evidence` as it stood, and when.
Append-only.

**Solver.** What works out what should happen: a person, or a model, or
an agent. A solver brings expertise and nothing else. It is the thing you
measure, and later the thing you may improve; `calibrate` is always of a
solver.

**Decider.** The solver that held **authority** for one decision — the
`decider` field on the record. Expertise and authority are deliberately
separate terms, because they are separate things: a solver may be
excellent and hold no authority, and authority always traces to an
accountable person. Granting a solver the authority to act unattended is
itself a decision, made by somebody, recorded like any other. There is no
unowned decision.

**Calibration.** For one solver on one condition, the rate at which its
choices were followed by the outcome those choices implied, and how long
it took. Computed from the record, never declared.

**Outcome.** Observed later. Never asserted. Stamped when the evaluator
stops reporting the situation.

**Precedent.** A prior `(decision, outcome)` for the same case identity.

**Group** *(proposed, not built)*. A claim ABOUT a set of cases — see
§5.

---

## 3. Operations, and which ones compose

This is the load-bearing section. The test for whether an operation can
recurse is not taste; it is whether the operation is a join on a
lattice.

**`rollup` (severity) — a join. Composes freely, at any depth.**
Severity is the worst member, which is a least upper bound on the status
lattice. That is the formal reason a case's severity is safe, and would
remain safe for a group of cases, and for a group of groups.

**`explain` (cause) — NOT a join. Needs a declared rule, and the rule is
scoped.** Today exactly one rule exists: a reporter that has gone quiet
cannot deliver anything else either, so other claims that are ALSO
merely silent are the same fault. A claim that is `failed` or
`unproven` is never collapsed this way, because its report did arrive
and its content was wrong — collapsing it would hide a second fault.
Cross-entity causation has no equally defensible rule and is therefore
not inferred.

**`reach` — a union, but not complete under composition.** A group can
touch something no member declares. A composed reach must say so rather
than implying the union is the whole.

**`decide` — fans out; the record stays at the leaf.** A decision about
many is how you act once; the recorded verdicts remain per-case, each
naming the group as its reason. This is not a style preference:
precedent, outcome observation and the whole calibration loop key on
stable per-entity identities, and a verdict that floated above them
would destroy the thing that makes the second occurrence cheaper.

**`observe` — level-triggered, never edge-triggered.** Cases are
recomputed from current state on every poll rather than mutated by
events. This is why identity is stable, why a lapsed hold simply
reopens, and why there is no reconciliation bug class here. It must not
be "optimised" into event handling.

**`calibrate` — an aggregate, and it composes only where the censoring
does.** For a `(solver, condition)` pair: of the cases where it chose
`hold`, how many cleared with nothing acting on them; of the cases where
it chose `fix`, how many resolved, and how long it took.

It composes across cases and across sites, which is what makes an
aggregate service possible at all — but only across records whose
censoring matches. Held and acknowledged cases have no observed outcome,
so a rate computed over decided cases is a **bound, not an estimate**, and
a surface that prints it as a point value is making exactly the claim this
construct exists to prevent. The bound is the honest number until a
censoring-aware estimator exists (§8).

The operation never reads the solver's *type*, and that is the reason it
is worth naming as an operation rather than a report: **evaluating a model
is this same computation with the solver swapped.** A benchmark, a
person's calibration curve, and the gate that decides whether something
may run unattended are one arithmetic over one record, not three
features.

---

## 4. Invariants

Each of these is enforced by a named test, listed beside it. A change
that breaks one is a change to the construct and needs an ADR, not a PR.

The construct's own rule applies to this list: a claim shows its
working. `test_construct_invariants.py` fails if any test named here
stops existing, so the table cannot quietly become aspirational.

1. **Identity survives re-derivation.** The same situation composes to
   the same `case_id` on every poll, across restarts and across sites.
   — `test_cases.py::test_case_ids_are_stable_across_composition`
2. **Outcomes are observed, never asserted.** No surface, verb or agent
   may write an outcome. It is stamped when the situation stops being
   reported.
   — `test_verdicts.py::test_outcomes_are_observed_not_asserted`
3. **Absence is stated, never blank.** "Not measured", "0 declared
   dependents", "no outcome yet", "not yet watched here" — each is a
   sentence. A blank field is a bug.
   — `test_reach.py::test_nothing_declared_reads_as_zero_declared_not_unknown`
4. **Every claim can show its working, including what it cannot
   establish.** A derivation without a `limit` is incomplete.
   — `test_derivation.py::test_a_derived_claim_says_what_it_cannot_establish`
5. **No affordance the server will not honour.** Options are composed
   server-side; the surface renders them and invents none.
   — `test_remedies_exist.py::test_every_declared_remedy_names_a_capability_that_exists`
6. **The record is append-only.** A change of mind is a new record. A
   correction is a new record. Nothing is edited.
   — `test_verdicts.py::test_history_is_append_only_and_newest_first`
7. **A rule that groups or collapses must be declared, narrow, and
   carry its limit.** The system does not guess at causation.
   — `test_cause.py::test_content_faults_are_never_collapsed_into_silence`
8. **What runs, runs through the gateway.** No surface invokes a
   capability privately; the guard, the site rules and the audit chain
   apply to every path.
   — `tests/infra/test_skill_dispatch.py` (the ast walk over direct calls)

---

## 5. Grouping (proposed)

The open question is whether cases can group cases for scale. The answer
is yes, and the shape is NOT a recursive `Case`.

A `Case` is about an entity — its identity, title, remedy applicability
and drill-down all key on one. A group is about no entity. Making
`Case` recursive forces a synthetic entity and a special path through
four subsystems.

Instead, **a group is a claim about a set of cases**: it references
member `case_id`s, and like every other claim it carries evidence, a
rule and a limit. "These nine nodes stopped reporting within four
minutes of each other" is evidence; "therefore one thing broke" is a
guess, and the limit says so.

Decisions on a group fan out to the members (§3). The fan-out policy is
**declared per grouping rule**, not inferred — the lesson from OTP
supervision strategies, where `one_for_one` versus `one_for_all` is
something you state.

Depth is bounded rather than open: claims → case (one entity) → group
(one pattern at a site) → optionally a fleet-wide level. Each level
needs its own declared rule. Arbitrary depth means arbitrary rules,
which is the one thing this construct refuses everywhere else.

The only grouping rule computable from stored data today is *same claim
kind, same status, co-occurring in time*. Grouping by shared declared
dependency would be better and is blocked on the same gap reach already
surfaced: almost nothing declares what depends on it.

---

## 6. What we took, and from where

Read these rather than rediscovering them.

**PEP 654 (`ExceptionGroup` / `except*`).** The recursion question,
already solved. A group is a container but handling *splits* it: a
handler matches a subset, that subset is handled, the remainder
propagates. That is §3's fan-out. Python also separates `__cause__`
("this caused that") from `__context__` ("this happened during that") —
we have the first and nothing for the second, which is a known gap.

**OTP supervision trees.** Restart strategies are the "what does a group
decision do to its members" question, and the answer after decades is
that you *declare* it per supervisor. §5 follows this.

**Kubernetes Conditions and controllers.** The closest existing data
model to a Claim: `type`, `status`, `reason`, `message`,
`lastTransitionTime`. Two lessons they learned the hard way and we
independently arrived at: `Unknown` must be first-class rather than
absence, and controllers must be level-triggered. §3 and §4.3.

**Alertmanager.** The domain analog with the scars. Inhibition rules are
our cause chain; silences with mandatory expiry are our hold window
(they learned that a silence without an end becomes permanent
blindness); grouping keys are the grouping rule. The whole feature set
exists to prevent alert fatigue, which is the failure this construct is
also built against.

**W3C PROV.** `wasDerivedFrom` is the derivation; Entity/Activity/Agent
map to entity, run and decider. Already referenced by ADR-042.

**Natural deduction.** A derivation with inputs, a rule and a conclusion
is an inference rule, and `limit` is the premise discipline that proof
systems get right and dashboards get wrong. "Nothing arrived" does not
entail "the node is down", and the surface marks the non-sequitur rather
than letting a reader make it.

---

## 7. What has no analog, and what follows from it

Exceptions do not have outcomes that arrive later. Alerts resolve, but
nobody records who decided what and whether it worked. Sagas compensate
but accumulate nothing. The piece with no clean computing analog is the
`(decision, outcome)` pair, keyed on an identity that survives
recurrence, accumulating into something that changes future behaviour.

The nearest analog is not in computing. It is an experimental record:
hypothesis, intervention, observed result, repeated, and the
accumulation is the asset.

That has a consequence worth stating plainly, because it changes what
the record has to be.

### 7.1 The record is a dataset by construction

Every decided case already emits, without any additional work:

- the **state**: claims with status, evidence, timestamps
- the **action space**: `options` — including the ones NOT chosen, which
  most decision logs never record
- the **choice**, attributed, with the evidence as it stood
- the **intervention**: what ran, and what it returned verbatim
- the **observed outcome**, and the time to it
- the **derivation**: inputs → rule → conclusion, with its limit, which
  is step-level reasoning with checkable premises
- **refusals**, recorded as first-class by the action ledger

That is `(state, available actions, chosen action, observed outcome)`
with provenance — the shape offline evaluation and calibration work
wants, and unusually it carries counterfactual actions and negative
examples rather than only successes.

Four properties make it better than most such corpora, and all four are
consequences of invariants we already hold: outcomes are observed rather
than self-reported (§4.2), so there is no optimism leak; derivations
carry limits (§4.4), so what cannot be concluded is labelled; the record
is append-only (§4.6), so nothing is retroactively relabelled; and
refusals are kept, so the negative class is real.

### 7.2 What it is NOT, stated before anyone over-claims

**It is not pre-training scale, and calling it that would be exactly the
incongruence this construct exists to prevent.** A fleet of dozens of
nodes produces hundreds of decisions a year. That is evaluation and
calibration scale. Say so.

**It is censored.** Outcomes exist only for cases somebody decided. Held
and acknowledged cases have none. Training on it naively teaches
"everything a human touched resolved". Censoring must be marked in the
record, not dropped from it.

**It is one distribution.** One fleet, one operator's habits.
Generalisation is unproven and should be treated as unproven.

**It is reflexive.** A model trained on decisions this system recorded,
whose decisions this system then records, will launder its own priors
into evidence. The only defence is the one already built — outcomes
observed independently of the decider, and derivations that state their
limits — and it has to be designed for rather than assumed.

**It is governed.** A corpus built from partner sites is a governance
question before it is a technical one.

### 7.3 What "designing for it" actually requires

Nothing large, and mostly things the construct already wants:

1. A **versioned schema** for the emitted record, so a corpus gathered
   this year is readable next year.
2. **Censoring marked explicitly** — a case with no outcome is labelled
   censored, never silently absent.
3. **Split by time or site, never at random**, because adjacent polls of
   one situation are not independent samples.
4. **The evaluation set is the product.** Measuring whether a judgement
   is calibrated is more defensible than any model trained on it, and it
   is the same asset either way.

The governance half — what may leave a site, on what terms, and what is
never licensed — is ADR-133, decided before anything is collected rather
than after. The strategy and sequencing behind it are in
`docs/working/eval-corpus-strategy-2026-09-25.md`.

### 7.4 The solver may be a model, and the arithmetic does not care

The record keys on `decider`, not on `human`, and §2 separates the solver
that works something out from the authority to commit it. Nothing in §3 or
§4 assumes either is a person, and one consequence falls out immediately:
the four things a team needs in order to *improve a solver* are four things
the record already emits or already computes.

| What improving a solver needs | Where it comes from here |
|---|---|
| examples of the judgement, in its context | every decided case already is one (§7.1) |
| a label that is not self-reported | the observed outcome (§4.2) |
| a measure of whether the new one is better | `calibrate`, solver swapped (§3) |
| a bar for letting it run | the same measure, read as a threshold |

The last row already exists in code, for a remedy rather than for a solver.
`never_worked` holds a fix back until it has cleared this case once, and
thereafter lets it run unwatched — autonomy earned by track record rather
than granted by somebody deciding it is safe. The identical mechanism,
pointed at a solver instead of at a fix, is how a solver acquires
authority. That is the same gray area measured instead of adjudicated, one
level up, and it is why the two words are worth keeping apart: expertise is
demonstrated, authority is granted, and the grant is a record.

**What this does not make cheap.** A base model, and the compute to tune
it. The construct removes the dataset problem, the labelling problem and
the did-it-actually-help problem. It does not remove the training problem,
and saying otherwise would be the over-claim §7.2 exists to forbid.

**And the ordering matters more than the capability.** At the scale §7.2
describes, tuning is usually the wrong instrument: retrieval over similar
cases is cheaper, needs no training at all, and is the rung most records
are still short of. The value of `calibrate` is that it says which rung
you are on instead of leaving it to enthusiasm. The ladder and its
sequencing are in `docs/working/eval-corpus-strategy-2026-09-25.md`.

**What must exist before any of it is built.** An invariant §4 does not
yet carry: every decision traces to an accountable person, including the
ones a model solver makes under granted authority. It is absent from that
table deliberately — there is no test for it, and a row there without a
test is the false green §4's own guard exists to catch. It lands with the
code, and it needs an ADR, because `decide` is today reachable only by an
authenticated session principal, and that is precisely the property a
grant of authority to a model changes.

---

## 7.4 Human decisions are attestations (planned)

A verdict with a human decider records a person's act, and the platform
now has one record for that: the attestation
([ADR-142](../adrs/adr-142-attestation-is-the-record-of-accountable-human-acts.md),
[spec-attestation.md](spec-attestation.md)). The verdict row stays the
case's own typed decision receipt; the human act behind it is signed in
the platform book `cases`.

- **`CaseVerdict.attestation_id`.** A verdict whose `decider_kind` is
  `human` carries the id of the attestation that signed it (meaning
  `decided`, subject `case`). Rule and model deciders carry none; a
  machine decision is never rendered as "signed by".
- **One record, two views.** `decider_label` and `evidence` (the case as
  it stood) are the case-shaped view of the attestation's
  `signer.display` and `evidence` (what was presented). They are written
  from the same presentation, so they cannot disagree.
- **The decision is signed through confirmation.** The case card is the
  presentation ([ADR-144](../adrs/adr-144-a-person-confirms-exactly-what-is-recorded.md)):
  its R21 anatomy is the confirm card's anatomy — Primary (sign the
  proposed resolution) · Hold (keep it, unsigned) · Ask (correct it, or
  ask about it, scoped to the case) — with the docket keys `a` / `h` /
  `?`. Deciding a case is irreversible in the record, which is exactly
  the gate class for which R21 reserves confirmation.
- **Autonomy changes are signed.** A promotion to Auto ("Approved 5/5, no
  reversals — switch to Auto?") signs `delegated`, and a demotion signs
  `revoked`, in the platform book `autonomy`. The seat reads its ceiling
  from the newest signed record; ADR-135's gates are unchanged.
- **Observed outcomes may be human.** Invariant 2 stands: no surface
  asserts an outcome. A person who *observes* one signs `observed` in the
  `outcomes` book, and calibration treats it as a witness tagged
  human-observed, never as the stamp itself.
- **New populations for the brief.** Missed attestation obligations,
  cross-check discrepancies raised by consumer books, and broken
  attestation chains reach the oversight brief as oversight items through
  `receipts.sources.register_source` — the same seam every other
  supervised population uses, so they compose into cases rather than
  arriving as a second inbox.

Invariant 6 (append-only) applies to both records independently: a change
of mind is a new verdict *and* a new attestation.

---

## 8. Open questions

- No `__context__` equivalent: claims that merely co-occur have no
  representation distinct from claims that are causally linked (§6).
- Grouping (§5) is designed and not built; the only computable rule
  today is co-occurrence in time.
- Cross-site levels are unspecified because the scale they would serve
  is unknown.
- `verify` commands exist only for the staleness claim; every other
  derivation currently says no independent check exists, which is honest
  but thin.
- The reflexivity defence (§7.2) is argued, not tested. There is no
  guard today that would catch a model's priors being read back as
  evidence.
- `calibrate` has no censoring-aware estimator. The rate over decided
  cases is a bound (§3); nothing computes an interval, and nothing stops a
  surface rendering that bound as a point value.
- No model has ever been a decider here. The vocabulary admits one (§7.4)
  and the authorisation path does not.
- Claims carry an entity *kind* but no *layer*. A surface therefore cannot
  show one altitude and suppress another, so a person may be handed a case
  from well below the abstraction they work at — or well above it. Likely
  an ADR, and likely a shallow canonical spine that a deployment maps its
  own names onto, rather than an industry stack hardcoded here.

_Copyright (c) 2026 The University of Texas at Austin and B-Tree Labs.
Apache-2.0 licensed._
