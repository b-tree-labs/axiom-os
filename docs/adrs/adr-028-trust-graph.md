# ADR-028: Trust Graph

**Status:** Accepted (as-implemented)
**Date:** 2026-04-17
**Authors:** Benjamin Booth, Claude
**Related:** ADR-026 (ownership model), ADR-022/023/024/025 (federation), `project_memory_architecture_unified.md`.
**Implementation:** `src/axiom/memory/trust.py` (15 tests passing).

---

## Purpose

Define how principals express, propagate, and revise trust in Axiom's
federated memory network. This ADR captures:

1. The **data shape** (TrustRecord, TrustContext, TrustGraph,
   ObservationEvent).
2. The **resolution order** when answering "should I trust X?"
3. The **derived-trust algorithm** for transitive trust with
   proximity weighting.
4. The **default philosophy** — optimistic with adaptation.
5. The **adaptation loop** hook that feeds observations into
   human-reviewed trust adjustments.

---

## Decision

### Trust is scored, contextual, and private

- **Scored**: floats in `[0.0, 1.0]`, not binary allow/deny.
- **Contextual**: every record is bound to a `TrustContext` —
  a `(domain × maturity × classification)` bundle. "I trust MIT on
  reactor physics at the Facts-tier, unclassified" is a *different
  record* from "I trust MIT on medical imaging."
- **Private**: records are private to the trustor. Derived /
  aggregate scores may be shared with consent; raw assertions
  stay local. The `records_visible_to(principal)` query enforces
  this by filtering to records the principal authored.

### Trust targets follow ADR-026's (principal, role, context) triple

Role succession rebinds role-scoped records automatically via
role membership updates. Human-scoped records stay with the
outgoing human. The `apply_succession(graph, succession)` function
realizes this mechanism.

### Hierarchical resolution — most-specific wins

Order when answering "trustor's trust of subject in context":

1. **Explicit human-scoped** record `(trustor, subject, ctx)` → use.
2. **Role-scoped** record where `subject` is in role membership → use.
3. **Optimistic default** = `context.admission_threshold`.

No "unknown principal" trap — unknowns get the welcoming baseline.
If the baseline is too permissive for a sensitive context, operators
raise `admission_threshold` at the context level.

### Derived trust — EigenTrust-inspired power iteration

For transitive trust (A → B → C → ... → subject):

- Walk paths from trustor to subject up to `blast_radius_hops + 1`
  hops.
- Each hop multiplies by `α` (the context's `alpha_decay`).
- Path contribution = `α^depth × min_edge_weight_on_path`.
- Multiple paths sum additively (capped at 1.0) — **this is where
  proximity boost comes from**. Two independent paths of equal
  length converge to ~2× a single path's contribution.

Core insight (from Kamvar et al. 2003 *EigenTrust*): a principal
reachable through many independent honest paths is more trustworthy
than one reachable through a single chain. Byzantine-resistant in
the limit; a rogue chain can't unilaterally boost its own reputation
because other honest paths wash it out.

### Defaults encode "optimistic with adaptation"

| Parameter | Default | Meaning |
|---|---|---|
| `alpha_decay` | 0.8 | Trust through 4 hops still > 0.4 |
| `admission_threshold` | 0.3 | Accept content unless clearly suspect |
| `blast_radius_hops` | 1 | Penalize immediate trust only on breach |

Rationale: academia + research + open federation work via
presumed-competence. Zero-trust starts break adoption. When emergent
bad behavior is detected (adaptation loop), thresholds tighten
automatically; good behavior relaxes them back.

### Adaptation loop — observation-driven proposals

```
ObservationEvent(observer, subject, kind, weight, at)
    │ breach_detected, peer_rejection, content_accepted, content_endorsed
    ▼
[graph.record_observation(event)]
    │ passive log, never mutates trust scores directly
    ▼
[propose_adjustments(graph, subject)]
    │ surfaces proposals: {direction, reason, suggested_delta}
    ▼
Human review (UI) → accepted proposals become TrustRecord deltas
```

**No auto-blacklist.** Every score change crosses a human checkpoint.
Prevents adversarial flooding of observation events from
programmatically demoting an honest principal.

---

## Rationale

### Why scored, not binary

Binary trust is a step function — a tiny misstep drops a peer from
fully-trusted to zero. Real trust degrades smoothly. Scored trust
matches intuition and supports graceful adaptation.

### Why contextual

"I trust MIT" is meaningless. "I trust MIT on fusion research,
Facts-tier, unclassified" is meaningful. Context bundles prevent
false-transitivity: high trust in one domain doesn't bleed into
unrelated domains.

Ben's constraint on tuning: per-fragment doesn't scale; per-context
does. The user tunes a small number of contexts (per domain pack)
and inherits defaults otherwise.

### Why private by default

Trust is a commitment when positive and a judgment when negative.
Broadcasting "I trust Alice at 0.9" is fine; broadcasting "I trust
Bob at 0.1" is hostile. Private-by-default prevents hostile
disclosure; aggregate-with-consent lets communities build reputation
layers ethically.

### Why EigenTrust-inspired, not full EigenTrust

Full EigenTrust is a global stationary-distribution computation:
converges over the entire graph, requires coordinated iteration
across nodes. For MVP we use a *local* power iteration bounded
by `blast_radius_hops`. Same math shape; simpler implementation;
sufficient for federation sizes < 10k.

When we need global scores at larger scales, we swap the local walk
for a proper iterative solver without changing the API.

### Why optimistic defaults

Zero-trust-by-default kills federation adoption. Peers arrive
not-proven-bad, and we extend moderate trust. Breaches tighten.
Ongoing good behavior is the baseline, not the exception.

This choice is VERY intentional and must be respected in future
tuning — see [optimistic-with-adaptation note in the unified-memory
architecture memory].

### Why human-in-the-loop adaptation

Fully automated adjustment is vulnerable to adversarial observation
injection. ("Flood the system with breach_detected events about
@alice and she gets auto-demoted.") Human review breaks that attack.
The proposal engine is a suggestion; the trustor decides.

---

## Alternatives considered

| Alternative | Rejected because |
|---|---|
| Binary trust (deny/allow) | Doesn't degrade gracefully under mild breaches |
| Global trust score (one per principal) | Bleeds trust across unrelated domains |
| Public trust broadcasts | Hostile disclosure of negative judgments |
| Full global EigenTrust | Overkill for < 10k peers; coordination cost |
| Auto-mutating scores on observations | Adversarial observation injection |
| Zero-trust defaults | Kills federation adoption at the door |

---

## Consequences

### Positive

- **Scales to federation sizes Ben targets** (10k–100k) with local
  iteration + contextual scope.
- **Role-human decomposition** handles succession, retirement,
  reassignment cleanly (see ADR-026).
- **Adaptation without automation** balances responsiveness against
  adversarial observation injection.
- **Extension-owned contexts** match the extension-aware-RAG
  architecture — each domain pack ships its contexts.
- **Proximity boost via independent paths** is emergent from the
  algorithm; no special code path.

### Negative

- **Path-walk derived trust is O(branching^hops)** in the worst
  case. Capped by `blast_radius_hops` (typically ≤ 3). For typical
  trust graphs with moderate branching, fine; pathological
  densely-connected graphs need a fallback to full iterative solver.
- **Role membership is held in-memory** in this MVP. Production
  needs signed published role artifacts (ADR-027 federated memory
  handles the transport).
- **Privacy is local-only**. Cross-node consent flow for sharing
  aggregate scores is deferred.

### Migration

Existing code without trust records continues to work: resolution
falls to optimistic defaults. New code should attach trust contexts
to domain packs and start accumulating records naturally through
normal use.

---

## Open items

- **Global aggregate scores with consent** — the network-effect
  feature. Deferred to a later ADR once the adaptation loop has run
  in production long enough to trust its outputs.
- **Cross-context trust bleed** — should high trust in one context
  give a small lift in sibling contexts? (MIT-fusion → MIT-fission?)
  Currently no; may change with usage data.
- **Adversarial observation injection detection** — today we rely
  on human review. A future rate-limit / signature-check could
  provide automatic throttling.
- **Trust decay over time** — trust records don't expire (by design
  — only delegations expire). But stale records may be misleading.
  A gentle time-decay factor applied at resolution time could help.

---

## Test coverage

`tests/memory/test_trust.py`: 15 tests covering:
- TrustRecord / TrustContext shape + optimistic defaults
- Empty graph → optimistic default
- Explicit-human > role > default resolution order
- One-hop and two-hop derived trust with correct α decay
- Proximity boost from multiple independent paths
- Adaptation loop: record observation, propose adjustments
- Role succession: role-scoped records rebind to new occupant,
  human-scoped records stay with outgoing human
- Privacy: trustor-only view of records

Integration with ADR-026 ownership: `TrustTarget` comes from
`axiom.memory.ownership`, `role_succession` ceremony output is
consumed by `apply_succession`.
_Copyright (c) 2026 The University of Texas at Austin and B-Tree Labs. Apache-2.0 licensed._
