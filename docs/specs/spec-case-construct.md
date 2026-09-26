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
   — `test_blast.py::test_nothing_declared_reads_as_zero_declared_not_unknown`
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

_Copyright (c) 2026 The University of Texas at Austin and B-Tree Labs.
Apache-2.0 licensed._
