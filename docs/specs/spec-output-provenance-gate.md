# Axiom Output-Provenance Gate Spec

**Status:** Implemented (2026-09-17, unpushed) — `axiom.rag.provenance` + vendored serving copy; 9 tests green. Analytics-tool integration + coverage reconcile still pending (see `prd-deterministic-quantitative-answering.md`).
**Owner:** Ben Booth
**Created:** 2026-09-17
**ADR:** ADR-113 (deterministic quantitative answering) · **Related:** `spec-analytics-tool.md`, `spec-rag-retrieval-policy.md` (retrieval side), `prd-evals.md` (grounding metrics), ADR-079 (data-as-tool)

---

## 1. Problem statement

An LLM answer may state a specific quantity (a temperature, a power, a reactivity, a rod
travel — any domain-configured unit) that it did not obtain from evidence. Retrieval quality
does not prevent this: a well-retrieved turn can still fabricate a value. The gate is the
**output-side** guarantee: a stated quantity must trace to grounded evidence *this turn*, or
the answer is withheld. It complements the retrieval-side RPE (`spec-rag-retrieval-policy.md`)
and the eval-side metrics (`prd-evals.md`).

Per ADR-113 the gate **verifies provenance; it does not compute**. Rich math is produced by
deterministic tools (`spec-analytics-tool.md`) and the domain model tier; the gate confirms a
tool produced the value.

## 2. Inputs

- **answer** — the model's text for the turn.
- **corpus** — everything that legitimately grounds a claim this turn: tool results (JSON or
  text), retrieved evidence, and the configured authoritative constants.
- **tools_called** — the tools actually dispatched this turn (for the false-provenance check).
- **config** (`ProvenanceGateConfig`, domain-extensible):
  - `quantity_patterns` — regexes for the values that MUST be grounded (platform defaults +
    domain units, e.g. a consumer adds its power/temperature/reactivity/rod patterns).
  - `authoritative_values` — a tiny, exact set statable without evidence (published constants).
  - `provenance_claim_patterns` — phrasings that assert tool provenance ("from the X tool").
  - `non_gating_patterns` — claim shapes tracked but never failed.
  - `equivalence_scales`, `numeric_abs_tol`, `numeric_rel_tol` — value-equivalence matching.
  - `abstention` — the replacement text (see §7).

## 3. The support rule

A matched quantity claim is **supported** iff any holds:

1. **Authoritative** — its numeric core ∈ `authoritative_values`.
2. **In corpus** — its value matches a corpus number under `equivalence_scales` within
   tolerance (so "375 C" matches "375C"; a cents figure matches a dollars figure under a
   configured scale).
3. **Descriptive-statistic aggregate** — it matches `sum / mean / min / max / std` of a
   numeric **series** extracted from a tool result (§5). This is the only re-derivation the
   gate performs.

`count` is **not** admitted (small integers collide with unrelated values → laundering risk).
No Tier ≥2 re-derivation (regression, calculus, dynamical models): those values are admitted
only via rule 2 when a deterministic tool produced them (`spec-analytics-tool.md`). A Tier ≥2
number with no producing tool call is **unsupported** — correctly, the model should have
called the tool.

## 4. Checks & outcome

- **Unsupported quantity** — any gated claim failing §3 → block.
- **False provenance** — a `provenance_claim_patterns` match with empty `tools_called` (claims
  to have called a tool it did not) → block.
- **Outcome** — if any block condition fires, the answer is **suppressed** and replaced with
  `config.abstention`; a structured block record is emitted to the turn trace with
  `event`, `reason`, `unsupported[]`, `false_provenance`, `verbs[]`, `suppressed_answer`.
  Otherwise the answer passes unchanged.

## 5. Format-agnostic series extraction (normalize once)

To apply rule 3 regardless of how a tool returned its data, the gate extracts numeric series
from the corpus into canonical numeric lists before aggregating:

- **JSON list of numbers** — `[0.96, 1.02, …]` → the list.
- **JSON list of dicts** — group by key; each key's numeric column is a series.
- **CSV / delimited text / rendered table** — each numeric column is a series (header-aware
  when a header row is present; otherwise positional).
- Individual float tokens remain matchable under rule 2 as today.

For each extracted series (≥2 numeric items) the gate adds `sum, mean, min, max, std` to the
supported set. Extraction is bounded (a max-elements backstop) and never executes or parses
arbitrary code — it reads numbers only.

## 6. What the gate does NOT do

- It does not compute regressions, integrals, derivatives, or dynamical-model outputs — those
  come from tools (`spec-analytics-tool.md`) / the domain model tier and are admitted by
  provenance (rule 2).
- It does not accept arbitrary **subset** aggregates (a mean over a filtered subset). If a
  subset statistic is needed, the tool must return the subset series so its aggregate is
  derivable; the gate must not widen to guess subsets.
- It does not parse strings into instructions; §5 reads numeric values only.

## 7. Abstention message contract

The abstention text MUST:
- state plainly that the specific value is not available from grounded evidence this turn;
- **not** hard-code example tool names (they mislead when unrelated to the question);
- **not** instruct the user to "ask again to pull it" — if a tool exists it should already
  have run; if none does, that is a coverage gap (§8), not a user action.
It MAY point to a catalog/discovery capability. Domain layers supply their own wording via
`config.abstention`.

## 8. Coverage invariant (ties to ADR-113 §5)

Every `quantity_patterns` entry the gate can gate MUST have ≥1 registered capability that
produces it; otherwise an abstention is guaranteed for that quantity. A **reconcile check**
(gated unit pattern → ≥1 producing capability) runs in CI. Gaps surfaced by the block/abstention
trace are the live to-do list for the tool catalog.

## 9. Configuration & layering

- The platform ships default patterns; a **domain layer extends** `quantity_patterns`,
  `authoritative_values`, `provenance_claim_patterns`, and `abstention` via
  `ProvenanceGateConfig.with_patterns(...)`. The platform layer names no consumer.
- The canonical implementation is `axiom.rag.provenance`. Serving nodes that cannot import the
  full platform consume a vendored copy; it MUST be kept in sync (single source of truth =
  `axiom.rag.provenance`).

## 10. Determinism, trace, audit

- Same answer + same corpus + same config → same verdict.
- Every evaluation appends a trace record (pass or block) enabling the coverage loop (§8) and
  post-hoc audit.

## 11. Tests (TDD)

- authoritative constant → PASS; unrelated fabricated value → BLOCK.
- corpus value match under each configured scale/tolerance → PASS.
- `sum/mean/min/max/std` of a tool series, delivered as **JSON list**, **JSON list-of-dicts**,
  and **CSV/table** → PASS (format-agnostic).
- `count`-shaped value with no other support → BLOCK.
- Tier ≥2 value (regression/derivative) with **no** producing tool call → BLOCK; same value
  **with** an analytics-tool result in the corpus → PASS (rule 2).
- false-provenance phrasing with empty `tools_called` → BLOCK.
- arbitrary subset mean not present as a returned series → BLOCK.
- reconcile invariant: every gated unit pattern has ≥1 producing capability.
- abstention message contains no hard-coded tool examples and no "ask again" instruction.
