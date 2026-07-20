# cross-mem PRD — portable, cross-harness memory

**Status:** Draft
**Owner:** Ben Booth
**Created:** 2026-07-10
**Last Updated:** 2026-07-10
**Decisions:** ADR-087 (architecture), ADR-088 (memory-recall corpus)

**Document set** (the complete cross-mem doc surface):

| Doc | Role |
|---|---|
| ADR-087 | Architectural decisions D1–D10 |
| ADR-088 | `rag-memory` corpus; amends ADR-069 projection scope |
| This PRD | Features, stories, acceptance criteria — build-out spec; each feature section is written to convert to a future user-docs page |
| `docs/security/cross-mem-serving-boundary.md` | Serving-boundary security contract + threat model (review gate for new consumers) |
| `docs/reference/harness-memory-survey-2026-07.md` | Harness-memory landscape research backing the adapter design |

---

## Executive Summary

Every major agent harness ships its own memory, siloed per
`(provider, account)`. Users who switch accounts lose their context; users who
work across harnesses repeat themselves in each one. cross-mem makes Axiom
Memory the user-owned home for all of it: **absorb** each harness's native
memory (provenance preserved per source), **migrate** memory and session
checkpoints between accounts and providers, **sync** across harnesses, and
**serve** memory back into any harness as prompt context — behind a fail-closed
policy boundary and without degrading prompt-cache economics.

One sentence: *your memory, owned by you, available in every harness, safe to
serve anywhere.*

## Problem Statement

1. **Lock-in by memory.** Upgrading or switching a licensed account today means
   abandoning accumulated context. No harness vendor has an incentive to fix
   this; the user's substrate does.
2. **Fragmentation.** N harnesses means N partial memories of the same person,
   none complete, drifting independently.
3. **Recall quality.** Axiom fragment recall is structured filtering only — a
   memory phrased differently from the query is invisible. Memory must not be
   the weak link in the prompt chain.
4. **Hidden serving costs.** Naive memory injection invalidates provider
   prompt caches (exact-prefix matching), silently multiplying latency and
   token cost on every turn.

## Users & Stories

- **Account upgrader** (P0): "I'm moving from a personal to a work account with
  the same provider. I want my memories, context, and recent session
  checkpoints there, with nothing missed and an audit trail."
- **Multi-harness developer** (P2–P4): "I use two harnesses daily. Each should
  know what the other learned, without me maintaining two rule files."
- **Provider switcher** (P2): "I'm moving providers. My memory comes with me."
- **Privacy-conscious professional** (always): "My work and personal accounts
  must never bleed into each other, and my secrets must never appear in a
  prompt."
- **Team with an existing RAG** (P3): "We already run a retrieval pipeline.
  Memory should compose with it, not replace it."

## Features

### F1 — Account & provider portability (P0 core)
`axi memory export` produces a signed, portable bundle: fragments, session
checkpoints, manifest, audit slice (vault content opt-in with re-encryption,
never silent). `axi memory import --assume-principal` re-homes under the
destination identity via the ADR-026 dual-signature ceremony and re-signs under
the destination node key. Idempotent: re-import is a no-op.
*Acceptance:* export on source, import on destination → recall-parity probe
(same queries return the same fragments), audit-chain continuity, zero loss;
CLI subprocess smokes for both verbs; work↔personal cross-serve negative test.
*User-doc conversion:* "Moving your memory to a new account" how-to.

### F2 — Semantic memory recall (P1)
`recall(query, intent, filters)`: hybrid dense + lexical retrieval fused by
RRF over the `rag-memory` corpus (ADR-088), with cognitive-type / principal /
time-range pre-filters and recency/salience scoring per the RPE plan. Keyed
lookups replace scan paths on the existing read/forget surfaces.
*Acceptance:* recall answers paraphrased queries that structured filtering
misses (golden eval set); latency within the serving budget; `read()` behavior
byte-identical to today.
*User-doc conversion:* "How memory recall works" concept page.

### F3 — Harness absorption (P2)
Four adapters by memory model (markdown-hierarchy; local structured store;
vector/passage; cloud API) absorb each harness's native memory into the user's
store, stamped with `SourceOrigin`. Read-only against sources; app-owned
databases are never written. Dedup runs as tiered entity resolution: exact
auto-collapse, near-duplicate reversible merge (source witnesses preserved),
conflicts kept-both and queued — never silent loss.
*Acceptance:* absorb → re-absorb is a no-op; per-source extraction returns
exactly the fragments that entered from that source; conflict queue populated
on planted contradictions.
*User-doc conversion:* per-harness "Connect your harness" how-tos + a
supported-harness matrix (seeded by the reference survey).

### F4 — Universal serving (P3)
Any harness can consume memory as prompt context: MCP retrieval tools, plain
text for prompt templates, or a query endpoint usable from a user's existing
RAG pipeline (side-by-side blocks by default; opt-in rank-level RRF fusion;
never ingestion of the foreign corpus). All serving passes the fail-closed
boundary (security doc); embeddings never cross the boundary unless a consumer
opts into a per-`(model, dim)` space; a query-time embed endpoint spares
consumers from matching spaces.
*Acceptance:* policy-gate conformance suite (vault-never, unlabeled-deny,
error-deny, cross-account-deny, deployment-tier-deny) passes for every
transport; coexistence demo against a stock external RAG.
*User-doc conversion:* "Serving memory into your tools" how-to + security
overview page.

### F5 — Cache-friendly serving (P3, named feature)
Memory injection preserves provider prompt-cache hit rates by construction:
stable epoch-pinned preamble with a cache breakpoint, volatile per-turn recall
in the tail, byte-identical rendering of unchanged state, session injection
ledger, provider-aware breakpoint placement. On self-hosted serving, a stable
per-user preamble is a shared KV prefix across all of that user's concurrent
agents.
*Acceptance:* instrumented A/B on a cache-billing provider showing cached-turn
input-token cost within target of a no-memory baseline, vs the naive-injection
comparison; no mid-session instruction-file writes ever observed.
*User-doc conversion:* "Why cross-mem doesn't slow your prompts" page with the
cost math.

### F6 — Cross-harness sync (P4)
One-shot import (idempotent) and continuous bidirectional sync: hub-and-spoke
with the Axiom store as the single reconciliation point; change detection per
harness; echo suppression via the `SourceOrigin` idempotency key; streaming
conflict policy (default last-writer-wins by event time + review queue).
Write-back goes only through the authored instruction-file layer (AGENTS.md
primary, per-product rules files as fallbacks), only at session
boundaries/epoch rollover. The sync service ships under the platform
service-reliability contract, event-driven.
*Acceptance:* two-harness lock-step demo; kill-and-restart with no loss and no
echo storm; conflict stream lands in the queue.
*User-doc conversion:* "Keeping two harnesses in sync" how-to.

## Non-Goals

- Not a new store — the ledger stays authoritative; everything added is a
  rebuildable projection (ADR-087 D6).
- Not a generation-model router.
- Never pushes memory into third-party retrieval stores (no-push rule).
- Does not migrate KV-cache warmth (impossible by construction; destination
  pays one cold prefill — disclosed, not hidden).

## Metrics

- **Portability:** P0 recall parity = 100%; migration wall-clock; zero-loss
  audit continuity.
- **Recall:** golden-eval recall@k on paraphrased memory queries; p95 recall
  latency within serving budget.
- **Cache:** cached-turn input-token cost vs no-memory baseline (target:
  within a few percent) and vs naive injection (expect ~10× improvement on
  cache-billing providers).
- **Dedup safety:** zero silent-loss property violations; merge-reversal
  round-trip correctness.

## Dependencies & Risks

- Depends on: ADR-026 ceremony, ADR-056 skills, ADR-069/088 corpus model,
  ADR-070 projector seam, existing hybrid retriever (RRF/embeddings/FTS).
- **Risk:** harness-native store formats churn → adapters read file-convention
  layers where possible; app-owned DBs are best-effort read-only.
- **Risk:** cloud-locked memories (no API) limit absorption to authored-file
  layers or user-triggered exports — documented per harness in the survey.
- **Risk:** entity-resolution thresholds — mitigated by reversible merges +
  review queue; thresholds tunable without data loss.

## Rollout

P0 → P4 per ADR-087 D10; P0 (same-provider account port) is independently
shippable and is the first public-facing capability. The retrieval-fusion
orchestration extension is a named successor, strictly downstream; nothing
here depends on it.

_Copyright (c) 2026 The University of Texas at Austin. Apache-2.0 licensed._
