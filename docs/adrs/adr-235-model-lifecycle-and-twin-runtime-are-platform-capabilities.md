# ADR-235: The model lifecycle and the twin runtime are platform capabilities

**Status:** Accepted  •  **Date:** 2026-11-05  •  **Deciders:** platform owner

**Related:** [PRD: Model Corral and Twin Runtime](../prds/prd-model-corral-and-twin-runtime.md), [Twin concepts](../reference/twin-concepts.md), ADR-204 (ModSim providers), ADR-162 (watchers), ADR-170 (idle slots), ADR-182 (never down), ADR-197 (one switch runner).

## Context

The registry of models lives in one consumer and requires that consumer's fields. The hosting design written for that consumer places its host there too. Other domains need the same lifecycle: register a model, prove it, promote it, run it beside a physical system, replace it without downtime. They differ in vocabulary, evidence and model libraries, and they have no difference in mechanism.

The test used across the portfolio is to remove the domain noun and see whether a capability remains. For every part of this system except the credibility rules, the model libraries and site facts, one does.

## Decision

1. **Axiom owns Model Corral's core:** revisions of any kind including surrogates, lineage, lifecycle states, resolution by operating configuration, packages, and the promotion record on the approval gate.
2. **Axiom owns the twin runtime:** the host, the worker protocol, delivery, the switch sequence, the watchdog, the rails and the control boundary.
3. **The host never imports a model library.** A worker adapter does, in the worker's process. Adapters are registered providers and live outside Axiom.
4. **Registry state and placement are separate records.** A revision's state is a fact about the revision. Placement is a fact about one revision on one host.
5. **A domain layer supplies** an extension schema, a credibility profile, service-class names, operating-configuration variables and adapters. **A site layer supplies** values, inventory, bindings and limits.
6. **Replacement is mirrored, not split.** Stateful models cannot share traffic, so a challenger receives the same frames as the served revision, and only the served revision reaches consumers.
7. **Generality is proven, not asserted.** A reference model with no domain content, and later a second real domain, must pass the same conformance suite.

## Options considered

| Option | Why not |
|---|---|
| Keep the registry and host in the first consumer | Every other domain rebuilds them, and domain rules harden into the core |
| Registry in Axiom, host in each consumer | Delivery, switching and rails are identical work per domain |
| Put the composition library in the host | Binds the platform to one library and its dependency stack |
| Adopt a general model-serving product | Built for stateless requests on clusters, and our hosts are small, disconnected and stepped |

## Consequences

- The consumer's Corral module splits: generic core to Axiom, domain schema and generators stay.
- Lifecycle work starts in Axiom, and the first consumer migrates by extraction with its records preserved.
- The consumer's hosting ADR is amended: its library-in-a-worker design stands as that domain's adapter, and the host moves here.
- The first proof needs no controlled code or heavy model library, so delivery can start before those questions are answered.
