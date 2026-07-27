# ADR-030: Federated Inference

**Status:** Accepted
**Date:** 2026-04-19
**Authors:** Benjamin Booth, Claude
**Related:** ADR-016 / 020 / 022 / 023 / 024 / 025 (federation foundations), ADR-027 (federated memory), ADR-028 (trust graph), ADR-029 (federation composition — this ADR's meta-constraint)

---

## Context

Nodes differ in what they can run. A personal leaf has a small local model (llamafile) or uses cloud APIs. A platform node (a self-hosted node) hosts heavier open-weight models (e.g. Qwen on a shared HPC cluster) reachable only over its private network. A cleared node hosts an export-controlled provider that must never expose queries to public cloud. A peer institution's node may serve a specialty model (e.g., clinical-fine-tuned Llama) that other nodes want occasional access to.

Without federation, each node's inference capability is an island. Either every node duplicates every provider (doesn't scale, wastes cost, fragments compliance), or each user learns which node runs which model and routes manually (terrible UX, violates "enter through the end"). Neither is acceptable.

Federated inference lets a node's query **route to a peer node's provider** — with authorization, provenance, policy enforcement, and graceful fallback — so the network as a whole delivers better inference than any single node can.

---

## Decision

Axiom's federation substrate supports **federated inference** as a first-class dimension. Nodes may serve inference to peers, and routes into their providers are governed by the same four primitives that govern federated memory (per ADR-029): identity, trust, policy, and content-addressed records.

**What gets federated.** Any registered LLM provider on a node can be exposed as a federated endpoint. The node advertises capability (model family, model name, routing_tier, routing_tags, rate-limit budget, optional SLO) via the federation catalog.

**What does NOT get federated (in this ADR).** Fine-tuning, training job submission, streaming token-by-token pass-through under fine-grained chargeback, and inference over shared KV-cache state. Those are separate ADRs (the first two tracked as future ADR-031 / ADR-032; the latter two are infrastructure-specific and deliberately out-of-scope).

---

## Four-primitives conformance (ADR-029)

Every federated-inference mechanism reuses the shared substrate — no new addressing, no new trust graph, no new policy surface, no new distribution protocol.

### 1. Identity / addressing

Federated inference requests use the existing `axiom://` URI scheme:

    axiom://<node_id>/inference/<provider_name>

Requests are routed via the cohort registry (ADR-027). Provider capability advertisements are MemoryFragments (kind = `federated_provider`) signed by the serving node, so consumers verify authenticity via the same identity roots as federated memory.

### 2. Trust

Routing priority for an incoming inference request is weighted by the requester's EigenTrust score (ADR-028) in the serving node's context. Low-trust peers fall through to the rate-limit floor; high-trust peers may be granted reserved-capacity slots. Trust score updates flow through the existing trust graph — no inference-specific reputation system.

### 3. Policy

A federated inference call is governed by **both** policy engines, resolved in this order:

1. **Requester-local** policy (`π_u` for the calling user, `π_a` for the calling agent): determines whether this query *may leave the node* at all. An EC-classified query from a non-cleared user never federates to a non-EC peer, regardless of what the peer advertises.

2. **Provider-local** policy (`π_t` on the target node, plus `π_global` for the provider): determines whether the provider *may accept this query*. A peer's EC provider checks the requester's identity against its EC whitelist (via ADR-022 identity roots); a rate-limited provider checks usage; a classification-gated provider checks the query's declared classification tier.

Both policies must approve. Either-side denial yields a federation-decline event that is audited on the requester side (so the user learns why their query was refused) and not on the provider side (so serving-node policy information doesn't leak).

### 4. Content-addressed records

Every federated inference call produces three linked records:

- **Request fragment** (requester side) — MemoryFragment(episodic) with `fact_kind: "federated_inference_request"`, carrying `(requester_node, provider_uri, query_hash, policy_decision, timestamp)`. Signed by the requester node.
- **Response fragment** (requester side) — MemoryFragment(episodic) with `fact_kind: "federated_inference_response"`, carrying `(request_id, response_hash, provider_node, provider_signature, usage_tokens, latency_ms)`. Signed by both nodes.
- **Service fragment** (provider side) — MemoryFragment(episodic) with `fact_kind: "federated_inference_served"`, carrying `(request_id, requester_node, provider_name, policy_decision, usage_tokens)`. Signed by the provider node.

Records are hash-linked (each response references its request; each serve references both). The requester and provider can independently reconstruct the audit chain without shared state.

---

## Routing semantics

When a node needs inference, the router evaluates providers in this order:

1. **Local providers matching the request's tier + tags.** If a local provider can serve the query under current policy, use it. Federation is never forced when the query is locally satisfiable.

2. **Federated providers** the current cohort registry knows about, ranked by:
   - Trust score (ADR-028) of the serving node for the requester-principal's context
   - Advertised capability fit (model family match, routing_tier, tags)
   - Observed latency percentiles (from prior inference_served fragments)
   - Remaining rate-limit budget

3. **Stub fallback** — explicit "federation unavailable" response. Never silently degrades to a wrong-tier provider (e.g., never falls back from EC to cloud on failure).

---

## Graceful degradation

- **Peer unreachable:** record an `inference_unreachable` event, re-route to next-best peer, then stub.
- **Peer rate-limited:** record a `peer_rate_limit` event, back off per advertised retry-after, try next peer.
- **Policy deny:** the request fragment records the decline with reason code; the user-facing error surfaces the reason (e.g., "This query requires an export-controlled provider. The internal host is the configured provider but declined — you are not on its EC whitelist.").
- **Partial failure mid-stream:** if a streaming response fails mid-way, the requester-side response fragment records `stop_reason = "peer_stream_error"`. No synthetic completion is generated.

---

## Trust bootstrap

A fresh node can't federate inference if no peer has any trust record for it. Bootstrap mechanisms (consistent with ADR-028):

- **Vouching:** an already-trusted peer can attest to a new node, giving it a starting trust floor in the vouching peer's cohort.
- **Institution membership:** nodes inheriting trust from an InCommon-style federation identity (via ADR-022) start at an institution-floor trust score.
- **Optimistic defaults (α=0.8):** per ADR-028, unknown-peer queries are served at the rate-limit floor until trust records accumulate.

No inference-specific trust onboarding ceremony. Federated inference piggybacks on whatever ADR-028 already supports.

---

## What changes for existing code

- `axiom.infra.gateway.Gateway` gains a federated-provider resolution path that, when no local provider fits, consults the cohort registry for federated providers matching tier + tags. Existing caller signatures do not change — federation is transparent.
- `axiom.federation.catalog` gains a `federated_provider` fragment kind. Providers on each node are published by the existing federation publication flow (ADR-027).
- `axiom.infra.audit_log` gains `federated_inference_request/response/served` events. The audit chain is the same machinery used for retrieval audit (T0-1) and prompt-composition audit (T0-3); only the event type differs.
- Policy engine: no new policy primitives. The existing four-scope coordinate (`π_global`, `π_u`, `π_a`, `π_t`) already expresses both directions. Adds two standard profile names: `accept_federated_inference` (provider side) and `allow_federated_egress` (requester side).

---

## Consequences

### Positive

- **Institutional specialization composes with federation.** One institution hosts a self-hosted Qwen node; a second hosts a domain-fine-tuned model; a third hosts a classification-tier provider. A student at any of the three gets answers routed to the right provider, with provenance.
- **Cost and compliance both improve.** Organizations stop duplicating providers to work around compliance boundaries. Rate-limit budget becomes a shared resource pool.
- **Audit trail per turn is complete.** Every inference is pinned to the node that served it, at the time it was served, with multi-party signatures. This is stronger than what any single-vendor platform can offer.

### Negative / costs

- **Latency:** a federated call is at minimum 1 network hop slower than local. For interactive chat this is acceptable (most paths remain local); for high-throughput batch jobs the router must prefer local.
- **Request audit volume triples (request + response + served).** Retention cascade (ADR-027 §TTL) applies — federation-inference audit fragments default to 90-day retention; high-compliance tiers persist indefinitely.
- **Peer abuse vector:** a compromised peer could use its trust score to consume rate-limit budget. Mitigation: serving nodes set per-peer rate limits in `π_t`; trust score degrades automatically on excessive rejection rate (ADR-028).

### Out-of-scope / deferred

- Streaming response multi-hop routing (single-hop only in v1).
- Inference-level chargeback / billing (not needed in the research-institution topology; revisit when a commercial peer joins a cohort).
- Cross-cohort inference (routing across federation boundaries) — a future ADR-033.
- Fine-tuning federation (ADR-031, pending).

---

## Alternatives considered

### Alt 1: Per-user API-key pool

Every user brings their own provider API keys; the node routes locally. Rejected — duplicates cost, doesn't solve private-network providers (a self-hosted node can't be reached by a random student laptop), doesn't solve classification boundaries.

### Alt 2: Central inference gateway

One institution runs a gateway that routes to all providers. Rejected — introduces a single-point compliance actor (violates federation principle), collides with ADR-028 trust graph (the gateway becomes a trust bottleneck), creates a commercial-grade service dependency out of a research platform.

### Alt 3: Full model-weight federation

Peers exchange model weights, every node runs every model. Rejected — storage prohibitive (multi-TB per model), legal (EULA issues for commercial models), compliance (model weights can themselves be export-controlled). This is a different problem tracked under ADR-031 (federated fine-tuning) which handles a narrower subset (LoRA adapter exchange).

---

## Implementation phases

**Phase 1 (read-only capability catalog, no routing):**
- `federated_provider` fragment kind published by each node
- `axi federation inference ls` shows peer-advertised providers
- Gateway unchanged — still local-only

**Phase 2 (optional federation, HITL confirmation):**
- Gateway router can be instructed to federate via an explicit flag (`routing_tags={"federate"}`)
- Request/response/served fragments land
- Policy decisions surfaced in audit but not yet enforced at the provider side

**Phase 3 (automatic federation with policy enforcement):**
- Gateway routes automatically when local provider unavailable for tier
- Provider-side policy enforcement fully active
- Trust-weighted ranking in place

Phase 1 should land before Prague (2026-07) — catalog-only adds value with minimal compliance risk. Phase 2 targets end-2026. Phase 3 targets 2027 after Prague cohort provides production data.

---

## References

- ADR-027: Federated Memory (request/response/served fragment pattern inspired by federated read fragments)
- ADR-028: Trust Graph (EigenTrust weighting, optimistic defaults)
- ADR-029: Federation Composition (the four-primitives constraint this ADR satisfies)
- T0-1 retrieval audit (`axiom/rag/retrieval_audit.py`) — proven precedent for signed per-call audit records
- T0-3 prompt-composition audit (`axiom/infra/prompt_observability.py`) — proven precedent for CompositionService-or-JSONL fallback writer
_Copyright (c) 2026 The University of Texas at Austin and B-Tree Labs. Apache-2.0 licensed._
