# Compute Decomposition (ADR-040)

Phase A scaffold of the platform-tier primitive. See:

- `docs/adrs/adr-040-compute-decomposition.md`
- `docs/specs/spec-compute-decomposition.md`
- `docs/prds/prd-compute-decomposition.md`

## What Phase A delivers

| Surface | Status |
|---|---|
| Closed pattern vocabulary (6 names) | shipped |
| Canonical invariant lists per pattern | shipped (declarative) |
| `embarrassingly_parallel` decomposer + recomposer + kernel | shipped |
| `Decomposer` / `Recomposer` Protocols | shipped |
| Trait routing decision table | shipped (pure function) |
| Federation directory record types (`COMPUTE_OFFER`, `COMPUTE_CLAIM`, `COMPUTE_RESULT`) | schemas shipped, gossip wiring deferred |
| Per-leaf runner (LocalDispatcher + SubprocessDispatcher via `infra.tasks`) | shipped |
| `aggregate_results` + optional `compose_memory_fragment` | shipped |
| `axi compute {decompose, dispatch, aggregate, peers, offer status}` | shipped |
| spatial / temporal / matrix / map_reduce / composite | invariants only; impl Phase B/C |
| LLM-proposed plans + verifier callbacks | Phase B |
| Sandbox profile + adapter attestation pipeline | Phase B |
| Sci Displays figure auto-render + paper drafter | Phase B/C |

## Quickstart

```bash
python scripts/demo-fmp-phase-a-scaffold.py            # in-process
python scripts/demo-fmp-phase-a-scaffold.py --subprocess --n 200 --chunks 4
```

## Extension authoring

Domain extensions register parameterizations against the closed
patterns. Minimal shape:

```python
from axiom.compute_decomposition import (
    register_pattern_parameterization,
)

register_pattern_parameterization(
    pattern_name="embarrassingly_parallel",
    parameterization_name="my_domain_batches",
    decomposer=MyDecomposer(),
    recomposer=MyRecomposer(),
)
```

A domain consumer (e.g. a Fleet Compute extension) registers its own
parameterization (e.g. `stochastic_transport_batches`) against `embarrassingly_parallel`.

## Tests

```bash
pytest src/axiom/compute_decomposition/tests/ -v
```

44 tests, all passing as of Phase A merge:

- `test_registry.py` — closed vocabulary + register/lookup/conflict
- `test_protocols.py` — Decomposer/Recomposer Protocols + auto-IDs + frozen types
- `test_directory_records.py` — schemas + TTL + round-trip
- `test_trait_routing.py` — full §10 decision-table coverage
- `test_embarrassingly_parallel.py` — round-trip vs closed-form ground truth
- `test_e2e_local_round_trip.py` — full pipeline through LocalDispatcher and SubprocessDispatcher
