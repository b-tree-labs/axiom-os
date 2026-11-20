# Spec: web app build-out, P6 Model

**Owner:** platform (web) • **Status:** Draft, hand-off • **Last updated:** 2026-10-30 • **Size:** XL, split into P6a · P6b · P6c
**PRD:** [prd-model-loop.md](../prds/prd-model-loop.md) (nouns, verbs, the three axes, the pipeline view, notebooks, platform ties); the product's specialization lives in its own repo
**Roadmap:** [spec-webapp-buildout-roadmap.md](spec-webapp-buildout-roadmap.md)

## Entry criteria

- P1 gate (Chart's catalog, Library, RAG, the data platform). P6 does not need P3–P5
  and may run beside them within the three-worker limit.
- The model registry and run record on `main` (verify the merged state of the registry
  and the run-record work it references).

## Design slice

| Source | Sections |
|---|---|
| PRD | §13 (Model and Chart: the builder's item, notebooks for agents, Chart as a sibling); §5.5 (the improvement loop); §14.2 (Model's drill-down) |
| Spec | §7.2 (model selection by estimated configuration); §7.4 (the improvement loop); §8.4 (notebook cards) |
| Gallery | v4 (Model and Chart); v5 "improve" screens; the generic improvement-loop diagrams |

## Scope

1. **Model, the builder's item** (a product may rename it): find or create a model →
   input deck → bind code(s) → schedule and monitor runs → tagged, versioned datasets →
   train a surrogate through generic interfaces → validate → promote along the
   model / shadow / twin ladder.
2. **The registry** and data kits (analyses, alerts, agent skills) live here.
3. **Agent notebooks:** cards that are agent commands (deterministic tool calls) or
   code, mixable, replayable, versioned (spec §8.4).
4. **The improvement loop:** residuals → hypothesis targeting a loss → modified input →
   runs → new training data → candidate → validated against the targeted loss →
   promotion; lineage backward from a prediction and forward from a hypothesis.
5. **Chart stays a sibling.** A model's outputs are charted through Chart's catalog.

**Out of scope.** New physics codes; GPU scheduling beyond the existing compute
extension.

**Components reused.** DataTable, charts, chart slots (P4 if landed, else links),
`Derivation`, `ProofBadge`, notebook card components from the as-built previews; Axiom
model registry, `compute`, `schedule`, `research`, lineage.

**Flags.** `model=on` (was `soon`); `model.notebooks=on`; `model.promote=on` for model
owners.

| ID | Acceptance | Trace |
|---|---|---|
| P6-A01 | A model goes from deck to a validated candidate entirely from the page, and the same steps run from CLI and MCP | §13.2, ADR-072 |
| P6-A02 | A notebook replays deterministically: the same inputs give the same outputs and the same run record | spec §8.4 |
| P6-A03 | Promotion requires a validation against the targeted loss and is signed on the card | §5.5, P2-A01 |
| P6-A04 | A prediction's lineage reaches its model revision, configuration and training data | spec §7.4 |
| P6-A05 | Empty Model offers a starter (a sample model and notebook) | §14.8 |

## Deploy and verify

Release; staging `model=on` for the site variant's model-owner persona; run one
improvement cycle on a fixture.

**Rollback.** `model=soon`; registry records persist.

**Estimate.** XL, as three packages that each land on their own: **P6a** registries and
runs (M–L); **P6b** train, V&V and promote (L; needs P2.0); **P6c** monitor and improve,
including notebooks (L). The model-loop PRD's acceptance (ML-01 … ML-08) is split across
them.
