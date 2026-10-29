# Memory That Federates

## A Four-Primitive Substrate for Cross-Institutional LLM Agents

**Product:** Axiom
**Copyright holder:** B-Tree Ventures, LLC (per ownership-map.md)
**Authors:** Benjamin Booth (B-Tree Ventures)
**Status:** Placeholder — see project_paper_portfolio_titles memory
**Target venue:** SOSP / OSDI (primary) · NeurIPS systems track (secondary)
**Date:** 2026-04-20

---

## One-line contribution

Commercial LLM-memory products are structurally single-tenant; Axiom
shows that composing four primitives (content-addressed URI, EigenTrust-
inspired graph, four-scope policy coordinate, auto-scaled propagation)
produces an architecture that matches single-node memory benchmarks
and unlocks multi-node properties commercial stores cannot reach.

## Public-benchmark anchors

- **LongMemEval** (Wu et al. 2024) — 500 Q&A × 113k-token sessions
- **LoCoMo** (Maharana et al. 2024) — long-context conversational memory
- *Novel:* federated-memory benchmark introduced by this paper

## Scale-inflection hook

Single-node federation = parity with commercial. Multi-node (10 → 1k peers)
= the regime where only architecturally-federated systems can operate.
Prior math: V(n) ≥ n·C̄ for knowledge amplification across peers.

## Status

Outline not yet written. Existing rough draft at
`axiom-composition-emergence.md` has been retired — its formal
emergence claims (CL-1 through CL-6) will be revisited when this
paper's proofs section is written.

---

_Copyright (c) 2026 The University of Texas at Austin and B-Tree Labs. Apache-2.0 licensed._
