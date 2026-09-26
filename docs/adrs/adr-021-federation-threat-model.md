# ADR-021: Federation Threat Model & Defensive Principles

**Status:** Accepted  
**Date:** 2026-04-13  
**Deciders:** Benjamin Booth  
**Related:** ADR-016 (Multi-node federation), ADR-018 (self-hosted node public endpoint), ADR-020 (Federation identity & relationships)

---

## Context

Federation multiplies the attack surface of any single Axiom node. Attackers can target identity, corpus quality, reputation, resource availability, and information integrity. As federation scales beyond a trusted research consortium into broader academic and community participation, bad-actor resistance becomes a first-order architectural concern, not an operational afterthought.

This ADR enumerates threats and codifies defensive principles. Specific implementations are deferred to follow-on work; this ADR establishes the threat landscape and the architectural non-negotiables.

## Threat Model

| ID | Threat | Description | Severity |
|----|--------|-------------|----------|
| T1 | Identity spoofing | Attacker claims another party's platform identity or agent | Critical |
| T2 | Finding poisoning | Attacker injects false findings that propagate to honest nodes | Critical |
| T3 | Attribution fraud | Attacker claims authorship of another's contribution | High |
| T4 | Corpus poisoning | Attacker federates RAG content designed to subtly mislead | Critical |
| T5 | Trust graph manipulation | Attacker inflates reputation via collusion | High |
| T6 | Resource exhaustion / DoS | Attacker floods federation with queries, proposals, digests | Medium |
| T7 | Information warfare | Adversarial actor strategically injects misleading findings | Critical |
| T8 | Consent bypass | Attacker surfaces restricted content via inference or side channel | High |
| T9 | Eval gaming | Node claims passing evals without honest execution | High |
| T10 | Sybil attack | Single attacker creates many pseudo-orgs to influence consensus | High |
| T11 | Insider threat | Authorized user within an org abuses federation access | Medium-High |
| T12 | Long-term patient attacker | Good behavior for months, then exploits built-up trust | High |
| T13 | Context impersonation | Attacker on same node forges an active context they don't control | High |
| T14 | Affiliation forgery | Attacker mints fake affiliation assertions | Critical |

## Defensive Principles

### Must be in Phase 0

**P1: Cryptographic identity at every layer.**  
Platform identity, node identity, and org identity each have Ed25519 keypairs. Every federation message carries signatures from the three layers (node, context attestation, affiliation). Private keys never transit the network. Addresses T1, T13, T14.

**P2: Content-addressed, signed findings.**  
Every promoted finding has a content hash and a chained signature (contributor + verifiers + eval attestations). Rebroadcasting preserves the chain. Attribution fraud requires forging a signature — cryptographically infeasible. Addresses T3.

**P3: Evaluation gates on every promotion.**  
A federated finding does not enter local corpus until it passes local evals (which attackers cannot see or control). Local evals are the primary defense against corpus and finding poisoning. Addresses T2, T4.

**P4: Access tier enforcement at every hop.**  
Export-controlled content never leaves the originating node (cryptographically enforced by local gateway). Restricted tier content transits only to Partner relationships with explicit consent per item. Metadata may federate where content cannot. Addresses T8.

**P5: Rate limiting + circuit breakers per peer.**  
Each peer has a budget: queries/min, digest size/hour, proposal volume/day. Violations trigger circuit breakers that reject further requests until cooldown. Already partially in ADR-018; extend to full federation. Addresses T6.

**P6: Quarantine for new federation peers.**  
Newly added peers start in quarantine: findings are received and evaluated but not propagated, proposals are visible but not actioned, reputation tracking begins at zero. Quarantine releases after configurable time + demonstrated quality. Addresses T7, T12 onset.

**P7: Local reputation tracking (primitives only in Phase 0).**  
Each node maintains a local view of peer reputation: eval pass rate of their findings, contradiction rate, behavioral compliance. Reputation is locally computed, not globally trusted — my assessment of a peer is mine, not inherited. Addresses T5, T10 (resistance to Sybil collusion because collusion does not change my local view). Full algorithm deferred; primitive scaffolding in Phase 0.

### Can defer to later phases

**P8: Attestation + reproducibility for evals.**  
Eval runs produce signed attested transcripts (hash of suite + inputs + outputs). Peers can spot-check by requesting reproduction. Addresses T9.

**P9: Consensus-weighted promotion for highest-tier knowledge.**  
Promotion to federated community RAG requires validation from N independent peers with reputation above threshold. Sybil-resistant because reputation requires history. Addresses T10, T12 at propagation.

**P10: Explicit distrust / blocklist.**  
Nodes declare specific peers as distrusted. Distrust propagates within a Cluster (ADR-020) but not across Partner or Federated boundaries. No central distrust registry. Addresses T7 coordinated response.

**P11: Federation governance protocol.**  
Cross-cutting concerns (appeals, coordinated incident response, shared threat intelligence) handled by a governance layer modeled on DNS operator coordination or CA compromise response. Not in Phase 0; architecture must permit it.

### Anti-patterns we refuse to build

**A1: Global reputation scores.** Centralizes trust, breaks sovereignty, creates a target for gaming. Each node computes its own peer reputation locally.

**A2: Unconditional trust transitivity.** "A trusts B, B trusts C, therefore A trusts C" is how bad actors spread. Trust assessments are per-peer, locally made, never inherited unconditionally.

**A3: Auto-federation based on signals alone.** Humans make trust decisions; agents propose. New Partner or Cluster relationships always require human confirmation. This is a hard RACI rule.

**A4: Central authority for federation membership.** No single entity decides who is in the federation. Any node can federate with any peer willing to federate back. Bad actors are handled by local quarantine, evals, and reputation — not by gatekeeping.

## Phase 0 Implementation Commitments

From the Must-be-in-Phase-0 principles above, the minimum defensive scaffolding is:

1. Ed25519 keypairs generated for every node and every context (extends ADR-018 beyond public-endpoint scope)
2. Signed federation messages with the three-layer verification chain (ADR-020)
3. Content-addressed findings with chained signatures, stored in ArtifactRegistry
4. Local eval gate invocation before promotion into local corpus
5. Access tier enforcement at federation boundaries (already in place for ADR-018; extend to all federation messages)
6. Per-peer rate limiting and circuit breakers
7. Quarantine state as a federation relationship attribute
8. Minimal reputation tracking: eval pass rate + contradiction rate + behavioral flags, stored per-peer

Anything beyond this scaffold is post-Phase-0.

## Consequences

**Positive:**
- Federation is defensible from day one, not a security retrofit
- Defenses compose naturally with ADR-020 identity layers — threat model follows the identity model
- Principles rule out centralization early, preserving the peer-to-peer architectural integrity
- Clear minimum commitments make Phase 0 scope verifiable

**Negative:**
- Cryptographic layer adds implementation complexity in Phase 0
- Local reputation tracking requires per-peer storage + eval-gate integration
- Quarantine state complicates federation relationship lifecycle

**Neutral:**
- Most advanced threats (T9 attestation, T10 consensus, T11 insider monitoring) are deferred but architecturally permitted. We commit to "we will be able to build this" without committing to "we will build this now."

## Open Questions

Deferred:
- Exact reputation algorithm and weighting
- Quarantine release criteria (time? volume of validated findings? both?)
- Cross-Cluster distrust propagation policy
- Response protocol for suspected compromised peers

---

*See also: ADR-019 Node Profiles, ADR-020 Federation Identity & Relationships.*
_Copyright (c) 2026 The University of Texas at Austin and B-Tree Labs. Apache-2.0 licensed._
