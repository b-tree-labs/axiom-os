# Trust, By Construction

## Deterministic Authorization for LLM Agent Federations

**Product:** Vega
**Copyright holder:** The University of Texas at Austin (per ownership-map.md)
**Authors:** Benjamin Booth (an institution) — co-authors TBD
**Status:** Placeholder — see project_paper_portfolio_titles memory
**Target venue:** USENIX Security / IEEE S&P / ACM CCS
**Date:** 2026-04-20

---

## One-line contribution

Architectural policy enforcement — deterministic authorization as a
first-class layer below the model — provably outperforms retrofit
governance wrappers under policy-stress adversarial load, and is the
only posture that scales to multi-root institutional federation.

## Public-benchmark anchors

- **AgentDojo** (Debenedetti et al. 2024) — 97 attacks × 629 tasks
- **InjecAgent** (Zhan et al. 2024) — prompt-injection for tool-using agents
- *Novel:* multi-root federation benchmark introduced by this paper

## Scale-inflection hook

Single-root governance is commodity. Multi-root federation (10 → 100
institutional trust anchors, non-foreclosing cross-bridge classification
per ADR-022/023/024) is the regime where architectural approaches
are the only ones that work.

## Status

Outline not yet written. Source material:

- `axiom/docs/adrs/adr-022..025.md` — identity + threat model
- `axiom/docs/specs/spec-classification-boundary.md`
- `axiom/docs/specs/spec-security.md`
- `axiom/src/axiom/federation/` — implementation

---

_Copyright (c) 2026 The University of Texas at Austin and B-Tree Labs. Apache-2.0 licensed._
