# ADR-113 — Deterministic quantitative answering: the gate verifies provenance, tools do the math

**Status:** Accepted (2026-09-18) · Implementation: gate (D4), analytics tool (D2, Tiers 1–3), coverage reconcile (D5), and the first domain instantiation are BUILT + verified live in the first consumer deployment (2026-09-17); platform release/vendor sync pending. Coverage now scopes to the *installed* capability set per ADR-115. Phase tracker: `docs/working/program-trustworthy-chat-2026-09-17.md` in the site repo.
**Deciders:** Benjamin Booth
**Related:** ADR-079 (data-modality routing / tiered gold / data-as-tool — the trustworthy sources this depends on), ADR-071 (`llm` as first-class core primitive — the gateway/tiers), ADR-056 (skills as invocable functions — how a tool is registered), ADR-072 (capability projection — one skill, many surfaces), ADR-073 (registry-driven tool surface), `spec-output-provenance-gate.md` (the gate contract), `spec-analytics-tool.md` (the generic compute tool), `prd-evals.md` (the measurement + coverage side), ADR-115 (generic medallion answering — capability-scoped coverage + the platform/pack/site demarcation), `spec-chat-prompt-pipeline.md` (the turn-pipeline contract).

---

## Context

The assistant must answer quantitative questions about a domain's data ("what was the
average X over the last two weeks", "fit the drift in Y", "rate of change of Z") without
ever stating a number it did not actually obtain. Two forces collide:

- **No fabrication.** An LLM will confidently emit plausible-but-wrong numbers. The output
  **provenance gate** (`axiom.rag.provenance`) scans an answer for quantity patterns and,
  when a stated value has no supporting evidence this turn, suppresses the answer and
  substitutes an abstention. This is load-bearing safety.
- **Legitimate derivation.** Real answers routinely require arithmetic on grounded data —
  an average, a sum, a slope, an integral. These derived values do not appear verbatim in
  any tool result, so a naïve gate flags them as unsupported.

The gate's aggregate closure tried to bridge this by re-deriving `sum` and `mean` over a
JSON list-of-dicts. It is insufficient and points the wrong way:

- It only fires on one payload shape (a JSON list-of-dicts); a series delivered as CSV
  text, a bare numeric list, or a table is invisible to it, so the aggregate is rejected
  and a **correct** answer is suppressed. (Observed: an average-over-a-series answer, fully
  grounded in a tool-returned daily series, blocked because the series arrived as a CSV
  string preview.)
- The direction does not scale. Extending the gate to re-derive regressions, integrals,
  and — the reductio — differential equations means building a computer-algebra system
  inside a safety gate: unbounded, brittle, and impossible to trust.

The generative-vs-deterministic split the platform already espouses resolves this: the LLM
proposes; deterministic machinery computes and renders. A number should come from a
deterministic tool, not from the model's head — and the gate's job is then to confirm a
tool produced it, not to re-check the arithmetic.

## Decision

**1. The gate verifies provenance; tools do the math.** A stated numeric value is admissible
iff it is (a) an authoritative published constant, (b) present in a tool result or retrieved
evidence this turn, or (c) a descriptive-statistic aggregate the gate can re-derive from a
tool-returned series (the small safety net in point 4). Anything richer than a descriptive
statistic **must be produced by a deterministic tool** and is admitted by its tool
provenance — never by the gate re-deriving it. A fit, derivative, or model output the LLM
performed inline (no tool call) is **rejected**; that is correct — it should have called the
tool.

**2. Layer the math (who computes what).**

| Tier | Operations | Determinism source | Home |
|---|---|---|---|
| 1 Descriptive | sum, mean, min/max, std, var, median, percentile, range, count | numpy | generic **analytics tool** |
| 2 Trend / fit | linear + polynomial regression (slope, intercept, R²), moving average, rate-of-change, correlation | fixed least-squares method | generic **analytics tool** |
| 3 Calculus on samples | numerical derivative, integral, cumulative sum, interpolation | fixed quadrature / finite-difference on the sample grid | analytics tool (+ some domain verbs) |
| 4 Dynamical models | ODE / PDE requiring domain equations | a solver **+ a physics/domain model** | the **domain model tier** (ROM / digital-twin / physics-code extensions), NOT a generic tool |

Tiers 1–3 are **domain-agnostic** — any numeric series → one reusable analytics tool. Tier 4
is **not generic** (it needs the equations) and belongs to the domain model tier (ADR-079's
"data/model as tool"; the consumer's ROM). The gate verifies provenance for all four
identically.

**3. Determinism is reproducibility + provenance, not gate re-derivation.** A tool result
carries `{op, inputs/series ref, n, params, method, source}`; given the same inputs and
method it returns the same value bit-for-bit (fixed numerical methods, no randomness). The
gate trusts this stamp.

**4. The gate keeps only a descriptive-statistic safety net, format-agnostic.** For trivial
inline aggregates the model still does, the gate may re-derive **sum / mean / min / max /
std** over a numeric series it can extract from the corpus **in any format** (JSON list,
JSON list-of-dicts by key, CSV/table columns) and admit a value that matches one. It does
**not** admit `count` (small integers collide with unrelated values and would launder
fabrication) and does **not** attempt Tier ≥2 re-derivation.

**5. Coverage is a derived, checked property.** The set of tools a problem space needs is
assembled from: (i) a **quantity × operation matrix** (quantities from the data schema ∪ the
gate's unit patterns, crossed with the operation set: value-at · series · aggregate ·
compare-to-model · compare-to-limit · catalog/freshness); (ii) the **real question corpus**
(evals + logs); (iii) the domain's **recognized codes/standards** for the model tier. The
**gate-block + abstention trace is the live gap detector** — loop until it goes quiet. The
enforceable invariant: **no gated quantity without a producing tool** (a gated unit with no
producer guarantees an abstention). A reconcile check (every gated unit pattern → ≥1
producing capability) makes coverage a CI-checkable property.

## Consequences

- **Positive.** Correct aggregate/derivation answers stop being suppressed; the fabrication
  guarantee is preserved (aggregates must be of real tool series; richer math must be
  tool-produced). Rich math scales by adding tools, not by growing the gate. The generic
  analytics tier is built once and reused by every domain; onboarding a new problem space is
  "enumerate domain quantities + wire the domain codes/ROM." Coverage becomes measurable.
- **Costs / risks.** A new generic analytics tool to build and maintain (`spec-analytics-tool.md`).
  Model behavior must shift to *routing* aggregate/derivation asks to the tool rather than
  computing inline; the determinism directive in the system prompt and the gate's inline-rejection
  of Tier ≥2 enforce this. Subset aggregates (a mean over a filtered subset) are only admissible
  if the tool returns the subset series — the gate must not widen to accept arbitrary subset means.
- **Follow-ups.** `spec-output-provenance-gate.md` (gate contract), `spec-analytics-tool.md`
  (the tool), consumer instantiation of the domain grounding config and the domain model tier,
  and eval probes (`prd-evals.md`) that assert real aggregates PASS while fabrication and
  inline-fit both remain BLOCKED.
