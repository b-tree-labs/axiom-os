# Copyright (c) 2026 B-Tree Ventures, LLC
# SPDX-License-Identifier: Apache-2.0

"""Tests for the embarrassingly_parallel built-in pattern.

Round-trip property: decomposer(P) -> chunks; per-chunk stub kernel
runs deterministically; recomposer(chunks_results) == ground_truth.

The kernel here is "sum the squares of integers 0..N" sharded into
K equal-ish blocks. Trivially embarrassingly parallel; ground truth
is closed-form: N(N-1)(2N-1)/6.
"""

from __future__ import annotations

import pytest

from axiom.compute_decomposition.patterns.embarrassingly_parallel import (
    EmbarrassinglyParallelDecomposer,
    SumAccumulator,
    SumOfSquaresKernel,
    SumRecomposer,
    canonical_invariants,
)
from axiom.compute_decomposition.types import (
    ChunkResult,
    Problem,
    Trait,
)


def _make_problem(n: int, n_chunks: int) -> Problem:
    return Problem.create(
        description=f"sum squares 0..{n}",
        pattern_hint="embarrassingly_parallel",
        parameters={"n": n, "n_chunks": n_chunks, "kernel": "sum_of_squares"},
        submitter="@test:local",
    )


def _ground_truth_sum_of_squares(n: int) -> int:
    return n * (n - 1) * (2 * n - 1) // 6


def test_canonical_invariants_present():
    invs = canonical_invariants()
    inv_names = {i.name for i in invs}
    assert "independence" in inv_names
    assert "accumulator" in inv_names
    assert "stochastic_seed_discipline" in inv_names
    assert "round_trip" in inv_names


def test_decomposer_emits_n_chunks():
    decomp = EmbarrassinglyParallelDecomposer()
    problem = _make_problem(n=100, n_chunks=4)
    chunks = decomp(problem, registry=None)
    assert len(chunks) == 4
    # Ranges must tile [0, 100) exactly with no overlap.
    covered = []
    for c in chunks:
        lo, hi = c.parameters["range_lo"], c.parameters["range_hi"]
        covered.extend(range(lo, hi))
    assert covered == list(range(100))


def test_decomposer_chunks_are_deterministic_trait():
    decomp = EmbarrassinglyParallelDecomposer()
    problem = _make_problem(n=50, n_chunks=3)
    chunks = decomp(problem, registry=None)
    for c in chunks:
        assert c.trait is Trait.DETERMINISTIC


def test_kernel_per_chunk_matches_partial_ground_truth():
    kernel = SumOfSquaresKernel()
    # Range [0, 10) -> 0+1+4+9+16+25+36+49+64+81 = 285
    out = kernel.run({"range_lo": 0, "range_hi": 10})
    assert out["sum"] == 285


def test_recomposer_round_trip_matches_ground_truth():
    """The headline property: decompose -> per-chunk kernel -> recompose
    yields the exact closed-form ground truth."""
    decomp = EmbarrassinglyParallelDecomposer()
    recomp = SumRecomposer()
    kernel = SumOfSquaresKernel()
    problem = _make_problem(n=1000, n_chunks=7)
    specs = decomp(problem, registry=None)
    chunks = [s.to_chunk(plan_id="plan-x", seed=None) for s in specs]

    # Simulate per-leaf execution with the deterministic kernel.
    results: list[ChunkResult] = []
    for c in chunks:
        out = kernel.run(c.parameters)
        results.append(ChunkResult.synthesize(
            chunk=c,
            leaf_node_id="@test-leaf:local",
            output_payload=out,
        ))

    aggregated = recomp.aggregate(chunks, results)
    assert aggregated["sum"] == _ground_truth_sum_of_squares(1000)


def test_recomposer_is_order_invariant():
    """Critical: shuffling the result list must not change the aggregate
    (sum is commutative). The recomposer sorts by sequence_index."""
    decomp = EmbarrassinglyParallelDecomposer()
    recomp = SumRecomposer()
    kernel = SumOfSquaresKernel()
    problem = _make_problem(n=200, n_chunks=5)
    specs = decomp(problem, registry=None)
    chunks = [s.to_chunk(plan_id="plan-y", seed=None) for s in specs]
    results = [ChunkResult.synthesize(
        chunk=c, leaf_node_id="@x:local",
        output_payload=kernel.run(c.parameters),
    ) for c in chunks]

    a1 = recomp.aggregate(chunks, results)
    import random
    rng = random.Random(0xC0DEFACE)
    shuffled = list(results)
    rng.shuffle(shuffled)
    a2 = recomp.aggregate(chunks, shuffled)
    assert a1 == a2


def test_recomposer_rejects_missing_chunk_results():
    decomp = EmbarrassinglyParallelDecomposer()
    recomp = SumRecomposer()
    kernel = SumOfSquaresKernel()
    problem = _make_problem(n=50, n_chunks=4)
    specs = decomp(problem, registry=None)
    chunks = [s.to_chunk(plan_id="plan-z", seed=None) for s in specs]
    results = [ChunkResult.synthesize(
        chunk=c, leaf_node_id="@x:local",
        output_payload=kernel.run(c.parameters),
    ) for c in chunks[:-1]]  # drop the last result

    with pytest.raises(ValueError, match="missing"):
        recomp.aggregate(chunks, results)


def test_sum_accumulator_pure_function():
    """Accumulator must be a pure function of its inputs (commutative+
    associative over the result pool). Property test on small inputs."""
    acc = SumAccumulator()
    pool = [{"sum": 1}, {"sum": 2}, {"sum": 3}, {"sum": 4}]
    assert acc.combine(pool) == {"sum": 10}
    # commutative
    import random
    rng = random.Random(0xC0DEFACE)
    shuffled = list(pool)
    rng.shuffle(shuffled)
    assert acc.combine(shuffled) == {"sum": 10}
