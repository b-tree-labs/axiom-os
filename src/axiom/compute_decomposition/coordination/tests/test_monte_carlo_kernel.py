# Copyright (c) 2026 B-Tree Ventures, LLC
# SPDX-License-Identifier: Apache-2.0

"""Tests for the MonteCarloDartsKernel + MonteCarloPiRecomposer.

TDD-first: this file lands RED and is driven green by
``coordination/monte_carlo_kernel.py``.

What we test:

1. Convergence — large N drives the per-chunk hit ratio toward
   pi/4 (and the recomposer's pi_estimate toward math.pi).
2. Determinism — same chunk_id + (start, count) → same hits.
3. Independence — different chunk_ids → different hit counts
   (probability of collision on 10k samples is negligible).
4. Recomposer aggregation — sums hits + trials across chunks and
   computes pi_estimate = 4 * hits / trials.
5. Per-chunk payload shape — hits, trials, range_lo/range_hi.
"""

from __future__ import annotations

import math

import pytest

from axiom.compute_decomposition.coordination.monte_carlo_kernel import (
    MonteCarloDartsKernel,
    MonteCarloDecomposer,
    MonteCarloPiRecomposer,
)
from axiom.compute_decomposition.types import (
    ChunkResult,
    ContentRef,
    DecompositionPlan,
    Problem,
    Trait,
)


# ---------------------------------------------------------------------------
# Kernel: convergence + determinism
# ---------------------------------------------------------------------------


def test_kernel_converges_to_pi_at_large_n() -> None:
    """A single 200k-sample chunk lands within 0.01 of pi/4."""
    kernel = MonteCarloDartsKernel()
    out = kernel.run({
        "chunk_id": "chunk-converge-0001",
        "range_lo": 0,
        "range_hi": 200_000,
        "kernel": "monte_carlo_pi",
    })
    pi_estimate = 4.0 * out["hits"] / out["trials"]
    assert out["trials"] == 200_000
    assert abs(pi_estimate - math.pi) < 0.02, (
        f"single-chunk pi estimate diverged: {pi_estimate} vs {math.pi}"
    )


def test_kernel_deterministic_per_chunk_id() -> None:
    """Same chunk_id + range → bit-identical hits."""
    kernel = MonteCarloDartsKernel()
    params = {
        "chunk_id": "chunk-determ-0007",
        "range_lo": 0,
        "range_hi": 50_000,
        "kernel": "monte_carlo_pi",
    }
    a = kernel.run(dict(params))
    b = kernel.run(dict(params))
    assert a == b
    assert a["hits"] == b["hits"]


def test_kernel_different_chunk_ids_produce_different_hits() -> None:
    """Different chunk_ids must seed independently — hit counts differ."""
    kernel = MonteCarloDartsKernel()
    base = {"range_lo": 0, "range_hi": 50_000, "kernel": "monte_carlo_pi"}
    a = kernel.run({**base, "chunk_id": "chunk-A-0001"})
    b = kernel.run({**base, "chunk_id": "chunk-B-0002"})
    c = kernel.run({**base, "chunk_id": "chunk-C-0003"})
    # Independent seeds — at least one pair must differ.
    hits = {a["hits"], b["hits"], c["hits"]}
    assert len(hits) >= 2, f"all chunk_ids gave identical hits: {hits}"


def test_kernel_payload_shape() -> None:
    """The kernel emits hits, trials, range_lo, range_hi."""
    kernel = MonteCarloDartsKernel()
    out = kernel.run({
        "chunk_id": "chunk-shape-0001",
        "range_lo": 100,
        "range_hi": 300,
        "kernel": "monte_carlo_pi",
    })
    assert set(out.keys()) >= {"hits", "trials", "range_lo", "range_hi"}
    assert out["range_lo"] == 100
    assert out["range_hi"] == 300
    assert out["trials"] == 200
    assert 0 <= out["hits"] <= out["trials"]


# ---------------------------------------------------------------------------
# Recomposer: aggregation
# ---------------------------------------------------------------------------


def _fake_chunk_result(chunk_id: str, plan_id: str,
                       payload: dict) -> ChunkResult:
    return ChunkResult(
        chunk_id=chunk_id,
        plan_id=plan_id,
        leaf_node_id="@local",
        output=ContentRef.from_payload(payload),
        payload=dict(payload),
        elapsed_ms=1,
    )


def test_recomposer_sums_hits_and_trials() -> None:
    """Recomposer sums per-chunk hits + trials, then computes
    pi_estimate = 4 * total_hits / total_trials."""
    recomp = MonteCarloPiRecomposer()
    plan = DecompositionPlan.create(
        problem_id="prob-mc-test",
        pattern_name="embarrassingly_parallel",
        parameterization_name="monte_carlo_pi",
        chunks=[],
        seed_seed=None,
        proposer="user",
    )
    results = [
        _fake_chunk_result("c1", plan.plan_id,
                            {"hits": 750, "trials": 1000,
                             "range_lo": 0, "range_hi": 1000}),
        _fake_chunk_result("c2", plan.plan_id,
                            {"hits": 800, "trials": 1000,
                             "range_lo": 1000, "range_hi": 2000}),
        _fake_chunk_result("c3", plan.plan_id,
                            {"hits": 790, "trials": 1000,
                             "range_lo": 2000, "range_hi": 3000}),
    ]
    out = recomp.aggregate([], results)
    assert out["hits"] == 2340
    assert out["trials"] == 3000
    assert out["pi_estimate"] == pytest.approx(4 * 2340 / 3000, abs=1e-9)
    assert out["n_chunks_combined"] == 3


def test_recomposer_full_round_trip_converges_to_pi() -> None:
    """Decompose + run kernel per chunk + recompose: pi_estimate
    within 0.01 of math.pi at N=400_000 across 8 chunks."""
    decomposer = MonteCarloDecomposer()
    kernel = MonteCarloDartsKernel()
    recomp = MonteCarloPiRecomposer()

    problem = Problem.create(
        description="mc-pi roundtrip",
        pattern_hint="embarrassingly_parallel",
        parameters={"n": 400_000, "n_chunks": 8,
                    "kernel": "monte_carlo_pi"},
        submitter="@test:local",
    )
    specs = decomposer(problem, registry=None)
    assert len(specs) == 8

    plan = DecompositionPlan.create(
        problem_id=problem.problem_id,
        pattern_name="embarrassingly_parallel",
        parameterization_name="monte_carlo_pi",
        chunks=specs,
        seed_seed=None,
        proposer="user",
    )

    results = []
    for spec in specs:
        chunk = spec.to_chunk(plan_id=plan.plan_id, seed=None)
        out = kernel.run({
            "chunk_id": chunk.chunk_id,
            "range_lo": chunk.parameters["range_lo"],
            "range_hi": chunk.parameters["range_hi"],
            "kernel": "monte_carlo_pi",
        })
        results.append(_fake_chunk_result(chunk.chunk_id, plan.plan_id, out))

    aggregate = recomp.aggregate(list(specs), results)
    assert aggregate["trials"] == 400_000
    assert abs(aggregate["pi_estimate"] - math.pi) < 0.01, (
        f"round-trip pi estimate diverged: {aggregate['pi_estimate']}"
    )


def test_decomposer_covers_full_range() -> None:
    """The Monte Carlo decomposer tiles [0, n) into n_chunks blocks
    with no gaps and no overlaps."""
    decomposer = MonteCarloDecomposer()
    problem = Problem.create(
        description="coverage",
        pattern_hint="embarrassingly_parallel",
        parameters={"n": 1000, "n_chunks": 7,
                    "kernel": "monte_carlo_pi"},
        submitter="@test:local",
    )
    specs = decomposer(problem, registry=None)
    assert len(specs) == 7
    assert specs[0].parameters["range_lo"] == 0
    assert specs[-1].parameters["range_hi"] == 1000
    cursor = 0
    for spec in specs:
        assert spec.parameters["range_lo"] == cursor
        cursor = spec.parameters["range_hi"]
        assert spec.trait is Trait.STOCHASTIC
    assert cursor == 1000
