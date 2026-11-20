# ADR-029: Federation Architecture Composition (Meta)

**Status:** Accepted
**Date:** 2026-04-17
**Authors:** Benjamin Booth, Claude
**Related:** ADR-016 / 020 / 022 / 023 / 024 / 025 (federation foundations), ADR-026 (ownership), ADR-027 (federated memory), ADR-028 (trust graph). Governs future ADR-030 (federated inference), ADR-031 (federated fine-tuning, TBD), any future `federated-*` dimension.

---

## Purpose

Axiom's federation story extends across multiple dimensions:
memory, RAG corpora, trust, inference, fine-tuning, identity,
peer review, evaluation, and more. Each dimension is powerful
on its own. The real value is in their **composition** —
institutional specialization + trust-weighted routing +
policy-governed sharing + content-addressed distribution become
combinations no commercial provider can match.

That composition is only additive if every federation dimension
rests on the **same substrate**. If each dimension introduces
its own addressing scheme, its own trust graph, its own policy
surface, or its own distribution protocol, the combinatorial
space explodes into incompatible subsystems and the platform
becomes unmanageable.

This ADR establishes the architectural constraint that keeps the
combinatorics additive.

---

## Decision

### The Four Shared Primitives

Every federation dimension MUST be expressible as a
**resource type** that uses these four primitives. No dimension
introduces its own parallel substrate.

| Primitive | Role | Canonical implementation |
|---|---|---|
| **Addressing** | Name every federation-scope resource | `axiom://<node>/<resource-id>` (ADR-027) |
| **Trust graph** | Gate every cross-node operation | `(principal, role, context)` trust records with EigenTrust-style derived scores (ADR-028) |
| **Policy coordinate** | Enforce operation-level rules | Four-scope `(π_global, π_u, π_a, π_t)` (`axiom.memory.policy`) |
| **Propagation mode** | Distribute availability changes | `push` / `pull` / `gossip` auto-selected by cohort size (ADR-027) |

Any new federation dimension:
- Uses the `axiom://` addressing scheme with a resource-type
  discriminator (for example `fragment`, `model`, `corpus`, `role`).
- Participates in the existing trust graph by defining a suitable
  `context` for its operations and targeting principals with
  `(principal, role, context)` triples.
- Expresses rules via the existing policy coordinate, adding new
  rule keys as needed (for example `inference_scope`,
  `finetuning_participation`) but not new scopes.
- Distributes availability via the existing cohort registry and
  auto-selected propagation mode.

### The Rejection Test

A federation-dimension proposal is **rejected as architecturally
conflicting** if it requires:

1. A new addressing scheme (not expressible as `axiom://<node>/<resource>`).
2. A parallel trust graph with its own scoring or resolution.
3. A separate policy surface that users must tune independently
   of the existing coordinate.
4. A distinct distribution protocol with its own cohort concept.

The proposer's options when rejected:
- Rework the design to fit the existing substrate.
- Make a case for amending the substrate (requires a revision
  to this ADR — a high bar, but not impossible).

### Content-addressing unification

All federation-scope artifacts (fragments, corpora, models,
inference outputs, roles, attestations) use content-addressed
identifiers derived from canonical serialization. The existing
`axiom.artifacts.ArtifactRegistry` primitive is the canonical
provider; version chains work identically across resource types.

---

## Rationale

### Why a single substrate matters

Combinatorial power is the promise of federation. It only
materializes if the combinations are cheap to express. Consider:

> "Pull UT's NE pack and route synthesis queries to UT's
>  NE-tuned LLM."

If pack federation uses one addressing scheme and inference
federation uses another, this one-sentence operation becomes two
separate subscriptions, two trust configurations, two failure
modes, two operational surfaces. A second sentence adds a second
explosion. By the fourth or fifth combination, nobody uses it.

A single substrate turns the same sentence into:

```
subscribe axiom://example.org/pack-domain-v2.1  # RAG
subscribe axiom://example.org/model-domain-v7   # inference
# same trust graph, same policy, same propagation mode
```

The combinations are cheap.

### Why these four primitives

They cover the orthogonal axes of any distributed operation:

- **Addressing**: *what* is being referenced
- **Trust graph**: *whether* the reference is honored
- **Policy coordinate**: *how* the operation is constrained
- **Propagation mode**: *who learns about changes*

No fifth axis has emerged in the dimensions we've analyzed
(memory, RAG, inference, identity, trust, evaluation). If one
ever does, we revise this ADR.

### Why the rejection test is strict

Platforms collapse under feature accretion. "Just this once we'll
have a separate trust graph" becomes five parallel graphs within
two years. The rejection test makes that cost visible up-front —
proposers either fit their idea into the substrate or justify an
amendment. Both outcomes are better than silent divergence.

### How downstream ADRs apply this

| ADR | Dimension | How it composes |
|---|---|---|
| ADR-027 | Federated memory | `fragment` resource type; trust gates retrieval; policy gates writes; cohort registry propagates addresses |
| ADR-028 | Trust graph | The substrate itself; other dimensions consume it |
| ADR-030 (future) | Federated inference | `model`, `inference` resource types; trust context = `<domain>/<inference>`; policy rule keys `inference_scope`, `inference_routing_preference`; model-availability advertisements propagate via cohort registry |
| ADR-031 (future) | Federated fine-tuning | `training-round` resource type; trust context = `<domain>/<fine-tune>`; policy gates which institutions participate; round announcements propagate as pack updates |
| Future identity ADR | Federated identity | `role` and `attestation` resource types; already consumed by ADR-026 role succession |

---

## The emergent combinatorics (worked examples)

Worked examples show what the substrate unlocks. These are not
specifications of the downstream ADRs — they illustrate the
compositional power.

### Bundled institutional specialization

An institution publishes a coordinated bundle:

```
axiom://example.org/pack-domain-facts-v2.1   (RAG)
axiom://example.org/model-domain-tuned-v7     (inference)
axiom://example.org/role-domain-faculty       (trust target)
```

A peer institution subscribes once per URI. The same trust
record (trustor = `@peer:example-org`, target = `@home:example-org`,
context = `domain/facts`) gates retrieval and inference equally.
Cross-institutional curriculum portability becomes a list of
URIs, not a project.

### Trust-weighted ensemble inference

Query runs on three peers. Each peer returns a signed inference
output. The client combines them, weighted by the derived-trust
score from ADR-028's EigenTrust. No new reputation substrate;
the inference signer is just another principal the trust graph
rates.

### Export-control-gated fine-tuning

A fine-tuning round is announced as `axiom://example-host.example.org/training-round-2026q2`.
The policy coordinate's `classification_ceiling` rule gates which
institutions can participate (only those with attested posture).
The round's signed artifacts propagate via the cohort registry.
Zero new machinery — the EC gate from ADR-028 + classroom
federation applies directly.

### Pack-aware inference routing

RPE (spec-rag-retrieval-policy.md) emits a plan specifying
`sources=[pack-ne-facts-v2.1]` and `strategy="synthesis"`. The
inference router reads the plan, queries the cohort registry for
models fine-tuned on `pack-ne-facts-v2.1`, picks one, routes the
query. Composition of RAG federation × inference federation uses
only existing primitives.

---

## Consequences

### Positive

- **Combinations are cheap**, not combinatorial-in-operational-cost.
- **Single set of primitives to document, teach, debug**.
- **Extension authors inherit federation behavior** by targeting
  the shared substrate instead of inventing new infrastructure.
- **Future dimensions have a clear integration contract**: hit
  the rejection test, design within the four primitives.
- **Security surface is bounded**: one trust graph, one policy
  layer, one propagation model to audit.

### Negative

- **Some dimensions have to work harder** to fit the substrate.
  Federated fine-tuning, for example, wants ephemeral working
  artifacts that don't quite match the content-addressed
  fragment model. It will need to abstract cleanly as a
  `training-round` resource. Discomfort is the price of
  composition.
- **The rejection test has teeth**. Sometimes a good idea will
  not fit and will need substantive rework. This is intentional;
  platforms that skip this step collapse.
- **Amendments to the substrate are expensive**. Proposers who
  hit the rejection test and believe the substrate needs
  widening face a high-bar ADR revision. This is also
  intentional.

### Operational implications

- Every federation-dimension ADR must reference ADR-029 and
  show explicit mappings of its operations to the four
  primitives.
- Code review for federation-scope changes asks: *which resource
  type, which trust context, which policy rule keys, which
  propagation mode*? Missing answers block merge.
- New rule keys on the policy coordinate are additive; removing
  rule keys requires migration plans.

---

## Open items

- **Substrate amendment procedure.** This ADR doesn't specify the
  process for proposing substrate amendments. Deferred until the
  first attempt surfaces.
- **Resource type registry.** Currently, resource types (`fragment`,
  `model`, `corpus`, `role`) are documented in the dimension ADRs
  that introduce them. A central registry might be needed once
  there are more than ~10. Deferred.
- **Evaluation framework as a federation dimension.** Running
  evals across institutions is a natural next dimension; needs a
  dedicated ADR that applies ADR-029.
_Copyright (c) 2026 The University of Texas at Austin and B-Tree Labs. Apache-2.0 licensed._
