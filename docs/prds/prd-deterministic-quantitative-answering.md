# PRD — Deterministic Quantitative Answering

**Status:** Draft (2026-09-17)
**Owner:** Ben Booth
**Created:** 2026-09-17
**Design:** ADR-113 · **Specs:** `spec-output-provenance-gate.md`, `spec-analytics-tool.md` · **Measurement:** `prd-evals.md`

---

## 1. Problem & why now

Users ask the assistant quantitative questions about a domain's data — point values, averages,
totals, trends, rates, integrals, model predictions. The assistant must return the **correct**
number, **grounded** in real data, or say plainly it doesn't have it — never a fabricated value.
Today two failure modes bracket this: an ungrounded turn can emit a plausible wrong number, and
the safety gate that prevents that **over-blocks legitimate arithmetic** (a correct, tool-grounded
average was suppressed because it was computed, not verbatim). Both erode trust in exactly the
questions the platform exists to answer.

## 2. Users

- **Analysts / operators / researchers** asking quantitative questions against live data.
- **Reviewers** who need a measured, honest answer (including a clean abstention) rather than a demo.
- **Platform + domain teams** who must assemble and keep adequate the set of deterministic tools a
  problem space requires.

## 3. Requirements

- **R1 — Grounded or abstain.** Every stated quantity traces to a tool result, retrieved evidence,
  or an authoritative constant this turn; otherwise the answer is withheld with an honest abstention.
- **R2 — Aggregates & derivations work.** Averages, sums, min/max, std, regressions, rates, and
  integrals over grounded series return correct answers — produced by deterministic tools, admitted
  by provenance (not re-derived in a safety gate). See ADR-113 tiers.
- **R3 — No fabrication, preserved.** A number not derivable from grounded data — and any Tier ≥2
  result the model computed inline instead of via a tool — is still blocked.
- **R4 — Format-agnostic.** A series grounds an aggregate regardless of whether the tool returned it
  as JSON, CSV, or a table.
- **R5 — Deterministic & reproducible.** Tool-computed numbers use fixed, versioned methods and carry
  a provenance stamp; same inputs → same output.
- **R6 — Honest abstention.** The abstention names no misleading example tools and never tells the
  user to re-ask; a missing tool is a coverage gap, not a user action.
- **R7 — Coverage is measured.** The tool set for a problem space is derived (quantity×operation
  matrix + question corpus + domain codes) and enforced by the invariant *no gated quantity without a
  producing tool*, checked in CI; the gate's abstention trace drives the gap loop.
- **R8 — Per-domain assembly.** A new problem space is onboarded by enumerating its quantities and
  wiring its codes/ROM; the generic analytics + gate come from the platform unchanged.

## 4. Acceptance criteria (measured via `prd-evals.md`)

- A real average/total/min/max over a tool series returns the grounded numbers (not an abstention),
  with the series delivered as JSON **and** CSV/table.
- A fabricated quantity, and an inline (no-tool) regression/derivative, are both blocked.
- An honest abstention fires when no producing tool exists — and that gap appears in the trace.
- The coverage reconcile check passes (every gated unit has ≥1 producer).
- Analytics-tool results are deterministic and carry a complete provenance stamp.

## 5. Non-goals

- Tier‑4 dynamical models (ODE/PDE needing domain equations) in the generic tool — those are the
  domain model tier (ROM / digital-twin / physics codes).
- Symbolic algebra or free-text expression evaluation; ML training / model selection.

## 6. Rollout

Model-independent and breaks real questions today, so it lands before/with the model-tier (router)
work: gate change → analytics tool → coverage reconcile check → eval probes → domain instantiation
(the domain grounding config + domain model-tier wiring, authored with their code per the docs-with-code rule).

## 7. Install modes & capability-scoped coverage (2026-09-18)

The assistant works in three install modes, all first-class; the ground-or-abstain design is what
makes the lower modes *safe* rather than broken:

1. **Chat core** (no data platform): RAG + general knowledge + analytics over caller-provided
   series. Quantitative questions about the operator's data have no producing tool → clean
   abstention. Nothing fabricates.
2. **Chat + data platform**: the platform's generic answering surface (`gold.tables/describe/
   aggregate/series`, see [prd-data-platform.md → Generic answering surface]) plus the analytics
   tool answer aggregate/series questions over whatever gold tables exist — no domain code.
3. **Chat + domain pack(s)**: curated declared verbs add named quantities, units, exclusion
   rules, provenance boilerplate; the coverage map and eval battery ride the pack.

**Requirement — coverage scopes to the installed capability set.** The coverage invariant (§3)
is evaluated against *discovered* capabilities (ADR-072/073 registry), never a hardcoded verb
table: installing a pack grows the gated-quantity set with producers; uninstalling shrinks it to
honest abstentions. A serving layer that hardcodes its tool dispatch violates this PRD.
Decision record: ADR-115.
