# Axiom Analytics Tool Spec (deterministic series math)

**Status:** Implemented (2026-09-17, unpushed) — `extensions/builtins/analytics/` (core Tiers 1-3, skill `analytics`, `axi analytics` cmd, AEOS manifest); 34 tests green. Routing to it + domain instantiation is P3.
**Owner:** Ben Booth
**Created:** 2026-09-17
**ADR:** ADR-113 (deterministic quantitative answering) · **Related:** `spec-output-provenance-gate.md`, ADR-056 (skills as functions), ADR-072 (capability projection), ADR-073 (registry-driven tool surface), ADR-079 (data-as-tool), `spec-extension-layout.md` / `spec-aeos-1.0.md` (extension shape)

---

## 1. Problem statement

Answering quantitative questions requires arithmetic on grounded data — averages, totals,
trends, rates, integrals. Per ADR-113 the model must **not** compute these inline (it will
drift and be blocked by the provenance gate); it must call a **deterministic tool** that
computes the value reproducibly and stamps it with provenance, so the number is grounded.

This spec defines that tool: a **domain-agnostic** compute capability over a numeric series,
covering ADR-113 Tiers 1–3 (descriptive statistics, trend/fit, calculus on samples). Tier 4
(dynamical models / ODE-PDE requiring domain equations) is explicitly **out of scope** — it
belongs to the domain model tier (ROM / digital-twin / physics-code extensions).

## 2. Capability surface

Registered per ADR-056 (a skill function) and projected per ADR-072/073 to CLI, MCP, and the
agent tool surface (one implementation, many surfaces). Shape:

```
analytics(op: str, series: SeriesRef | list[number], *, params: dict = {}) -> AnalyticsResult
```

- **op** — one operation from §4.
- **series** — either an inline numeric list, or a `SeriesRef` naming a prior tool result +
  the column/key to use (resolved via §3). Multi-series ops (correlation, regression x~y)
  take two refs.
- **params** — op-specific (e.g. polynomial `degree`, moving-average `window`, percentile `q`,
  integral `method`).

`AnalyticsResult` is JSON: `{ op, value | values, unit?, n, params, method, source, series }`
— see §5. `value` is what the model quotes; the whole object is what the gate sees.

## 3. Series resolution (format-agnostic)

The tool resolves a `SeriesRef` to a canonical `(index, values)` series using the same
extraction contract as the gate (`spec-output-provenance-gate.md` §5): JSON list, JSON
list-of-dicts (by key), CSV/delimited text, or a rendered table. This is the single reason a
CSV-preview series and a JSON series behave identically. Missing/non-numeric cells are dropped
with a recorded `n_dropped`; an empty resolved series returns `value: null` with a reason (the
gate then reports "no data", never a fabricated value).

**A named column MUST exist.** When the caller names a column (a header name, a dict key, or an
integer index) and it cannot be found, the tool raises rather than resolving a different series.
Falling back to "the first numeric column" answers a question the caller did not ask, with a
number that looks real: a typo'd or renamed `peak` would return the mean of an id column and
nothing in the result would say so. This is the one failure this tool must never have, so it is
an error rather than a note. Column selection is exact — a case difference is a miss.

The heuristic default is unchanged and applies only when the caller names **no** column: the
first numeric column (text) or the first numeric-valued key (dicts).

## 4. Operations

**Tier 1 — descriptive.** `sum, mean, min, max, std, var, median, percentile(q), range, count`.
**Tier 2 — trend / fit.** `linregress` (slope, intercept, R²), `polyfit(degree)` (coeffs, R²),
`moving_average(window)`, `rate_of_change`, `correlation(x,y)`.
**Tier 3 — calculus on samples.** `derivative(method)`, `integral(method)`, `cumsum`,
`interpolate(at, method)`.

Every op is a fixed, documented numerical method (numpy / scipy); no heuristic model choice at
runtime. Where a domain already ships a specialized deterministic verb (e.g. an energy =
∫power·dt verb), that verb remains the preferred producer for its quantity; the analytics tool
is the generic fallback and the home for cross-quantity math.

## 5. Determinism & provenance (the stamp)

- **Deterministic:** same inputs + same `op` + same `params` → identical output, bit-for-bit.
  No randomness, no wall-clock, no ambient config affecting the number. The `method` field
  names the exact routine (e.g. `"scipy.stats.linregress"`) and is versioned with the tool.
- **Provenance stamp:** `AnalyticsResult` records `op`, `params`, `method`, `n` (points used),
  `source` (the originating tool/series), and `series` (the ref). The provenance gate admits
  the quoted `value` because a tool produced it — it does not re-derive it.

## 6. Routing & usage

- The system prompt's determinism directive routes any aggregate/derivation ask to this tool
  (or a domain verb), never inline arithmetic. The gate enforces this by rejecting inline
  Tier ≥2 values.
- **Composition:** a domain data verb returns the series; `analytics` computes over it. Example
  flow (domain-agnostic): `series_verb(range) -> series`; `analytics("mean", series_ref)`.
- **Tier 4 boundary:** questions needing domain equations (dynamical response, model
  prediction) route to the domain model tier (ADR-079 model-as-tool / the consumer's ROM), not
  here. This tool never imports domain physics.

## 7. Packaging

An AEOS extension per `spec-extension-layout.md` + `spec-aeos-1.0.md`; skill functions per
ADR-056; decision records under the extension's `docs/decisions/`. Domain-agnostic — it names
no consumer and ships in the platform.

## 8. Non-goals

- Tier 4 dynamical models / solving domain ODE-PDEs (domain model tier).
- Symbolic algebra / arbitrary expression evaluation from free text.
- Statistical inference beyond fixed descriptive/regression methods (no model selection,
  hyperparameter search, or ML training).

## 9. Tests (TDD)

- Each op: known series → known result (golden values); determinism (repeat → identical).
- Format-agnostic: identical result whether the series arrives as JSON list, JSON list-of-dicts,
  or CSV/table.
- Empty / all-non-numeric series → `value: null` + reason (no fabricated value).
- Provenance stamp present and complete on every result.
- End-to-end with the gate: an `analytics` result in the corpus makes the quoted aggregate PASS;
  the same value stated without the tool call is BLOCKED.
- Registry projection: the same skill is reachable via CLI, MCP, and agent-tool (ADR-072/073).
