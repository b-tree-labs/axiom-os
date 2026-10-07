# Spec: Decision providers

**Owner:** Ben Booth • **Status:** Draft • **Last updated:** 2026-10-05
**PRD:** [prd-regulated-data-regimes.md](../prds/prd-regulated-data-regimes.md) • **Key ADRs:** [ADR-152](../adrs/adr-152-the-semantic-classifier-advises-and-labels-lists-and-agreements-decide.md), [ADR-158](../adrs/adr-158-export-control-health-privacy-and-personal-data-are-regimes-of-one-mechanism.md) • **Parent spec:** [spec-governed-interaction.md](spec-governed-interaction.md) (C7 lanes and graduation, C9a the typed-decision engine)

## Overview

This is the consumer-facing contract for every place a statistical opinion or a graduation decision enters the governed-interaction framework. It exists so that the engine behind a seat is swappable: the platform's own typed-decision engine, a deployment's trained model, or an external product can stand behind the same interfaces, and nothing downstream can tell the difference except by reading the receipts. The first implementation of the provider side shipped on 2026-10-05 as a deployment's lane-choice seat (a local, stdlib-trained classifier running as a shadow candidate); this spec generalizes exactly what that implementation emits, so the schemas below are live shapes, not aspirations.

Two provider kinds are specified: **typed-decision providers** (they answer judge, choose and score calls at a seat) and **graduation-gate providers** (they consume a seat's record stream and answer what standing the seat has earned). Both are registered, versioned and replaceable; neither is ever reimplemented around.

## Contracts

### D1. Seat registration

A seat is the unit of consumption, calibration and graduation. A seat registers with:

| Field | Meaning |
|---|---|
| `seat_id` | stable name, kebab-case, e.g. `lane-choice`, `detector-advice:<regime>`, `eval-judge` |
| `decision_type` | `judge`, `choose`, or `score` |
| `options` | the closed option set (`judge` implies two; `choose` lists them; `score` gives the scale) |
| `deployment` | the site or node identity the calibration is scoped to |
| `consumer` | which PEP stage or subsystem reads the opinions |
| `mode` | `shadow`, `advisory`, or `active`; a new seat is always `shadow` |

A call to an unregistered seat is refused. Options are closed on purpose: a provider cannot invent an answer outside the set, and a seat cannot widen its own options without re-registering, which resets its standing to shadow.

### D2. The typed-decision provider interface

```
decide(seat_id, decision_type, input_refs, context) -> TypedDecision
```

`TypedDecision` carries: the chosen option (or probability, or level with distribution), a `confidence` in [0,1], the `provider_id` and `model_version` (a content hash of the artifact, `config_hash` style), `evidence_refs` (references, never content), and `latency_ms`. Hard rules, each testable:

1. **Closed output.** The option must be in the registered set; anything else is a provider fault and the consumer treats it as no opinion.
2. **Evidence beside confidence.** A decision with no `evidence_refs` is still valid but is marked `unevidenced`, and the record shape makes a confident unevidenced opinion visibly a guess. Confidence never substitutes for evidence.
3. **Content locality.** A provider only receives content its regime rules allow (ADR-158 processor rule). An external provider on a regulated seat is refused at registration, not at call time.
4. **Cheap or absent.** A provider that cannot answer inside the seat's latency budget returns no opinion; the consumer proceeds as if the provider were silent. Opinions are advice until the seat is active (ADR-152; parent spec C1).

### D3. The calibration receipt

Per seat, per deployment, per model version, regenerated on every training run and on a schedule thereafter. The schema below is exactly what the first implementation emits today:

| Field | Meaning |
|---|---|
| `seat` | the seat id |
| `model_version` | the artifact version the receipt grades |
| `n_train`, `n_holdout` | label counts |
| `holdout_accuracy` | accuracy on held-out labels |
| `state` | `ok`, `noisy` (too few or lopsided labels), `stale` (labels older than policy), `drifted` (recent live accuracy departs from holdout) |
| `bins` | stated-confidence vs observed-accuracy bins: `{lo, hi, n, stated_mean, observed_accuracy}` |
| `label_rule` | the versioned rule that graded outcomes into labels |
| `skipped` | counts of turns the label rule refused to label (`ambiguous`, `excluded`, `no_outcome`) |

Rules: a seat whose receipt is missing, `noisy`, `stale` or `drifted` contributes **nothing** to graduation evidence, however good its opinions look anecdotally. Receipts are content-free and live with the deployment's records; the label rule is versioned because changing how labels are graded invalidates comparisons across receipts.

### D4. The decision record stream

Every consumed opinion lands in the turn's decision record (parent spec C5): the live decider, each candidate's option and confidence, the `disagreements` list, and errors by provider. This stream, joined with graded outcomes, is the only input a graduation gate needs, and it contains no content. A gate that asks for content is asking for the wrong thing.

### D5. The graduation-gate provider interface

```
standing(seat_id, window) -> Standing
```

`Standing` carries: `mode` (`shadow` | `advisory` | `active`), the `evidence` that justifies it (sample size, success criterion, measured value, window, the calibration receipt reference), `reversible: true` (a gate that cannot demote is not a gate), and `gate_version`. Rules:

1. **Transitions are evidence-gated, never time-gated,** and are per seat per deployment: the same seat may be active at one site and shadow at another, from each site's own outcomes.
2. **Demotion is automatic on drift.** A `drifted` or `stale` receipt demotes an active seat to advisory at most, and the demotion is recorded like any transition.
3. **Every transition is a signed record** carrying its evidence, and `gate_version` is stamped into each subsequent decision record so an auditor can tie opinions to the gate that allowed them to matter.
4. **The default gate is a person** reading the lane/seat report; the API exists so a statistical gate product can stand behind the same seam.

### D6. The external-gate seam

A commercial graduation-gate product is approved for integration behind D5 (ownership conflict of interest disclosed and recorded by the owner). The integration consumes its client SDK as a provider; its function is never reimplemented in this codebase, the requirement is this spec, and the product remains replaceable by the default human gate without any consumer change. The same applies to external typed-decision services behind D2, which additionally face the content-locality rule: never on a regulated seat.

## Design

Providers register through the existing provider identity base (uid, `config_hash`) and the strategy/registry pattern already carried by the routing core; the shadow runner is the reference consumer. Training pipelines are deployment-local: artifacts whose vocabulary derives from user content never leave the deployment and never enter a repository. Scheduled re-receipts implement `stale`/`drifted`: a sweep re-grades recent live decisions against outcomes and regenerates the receipt without retraining.

## Decisions

- ADR-152 fixed that statistical opinions advise and never decide alone; D2.4 and D3's graduation rule are its enforcement here.
- ADR-158 fixed the processor rule; D2.3 applies it to providers.
- Parent spec C7/C9a fixed shadow-first graduation and the engine's shape; this spec is their contract surface.

## Open questions

1. Where standings live (the settings store vs the policy set) and who may call a transition besides the gate. Owner: Ben.
2. The drift detector's window and thresholds for `drifted`. Owner: framework implementer, from the first months of receipts.
3. Whether `evidence_refs` get a uniform reference scheme across seats (trace turn ids, logbook entries, document ids). Owner: framework implementer.
