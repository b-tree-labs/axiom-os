# Copyright (c) 2026 B-Tree Ventures, LLC
# SPDX-License-Identifier: Apache-2.0

"""End-to-end integration test for the embarrassingly_parallel
round-trip with a real local dispatcher backed by ``infra.tasks``.

This is the property the demo script exercises: decompose -> dispatch
each chunk through the persistent task runner -> collect results ->
recompose -> match closed-form ground truth.

We use the LocalDispatcher (in-process, subprocess-free) to keep the
test fast. The TaskStore is constructed against a per-test temp dir
so we don't pollute the user's $AXI_STATE_DIR.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from axiom.compute_decomposition.aggregator import aggregate_results
from axiom.compute_decomposition.patterns.embarrassingly_parallel import (
    EmbarrassinglyParallelDecomposer,
    SumOfSquaresKernel,
    SumRecomposer,
)
from axiom.compute_decomposition.registry import PatternRegistry
from axiom.compute_decomposition.runner import LocalDispatcher
from axiom.compute_decomposition.types import (
    ChunkResult,
    DecompositionPlan,
    Problem,
)


def _ground_truth(n: int) -> int:
    return n * (n - 1) * (2 * n - 1) // 6


def test_e2e_decompose_dispatch_recompose(tmp_path: Path):
    # 1. Register the parameterization for this test run.
    registry = PatternRegistry.with_builtins()
    decomposer = EmbarrassinglyParallelDecomposer()
    recomposer = SumRecomposer()
    registry.register_parameterization(
        pattern_name="embarrassingly_parallel",
        parameterization_name="sum_of_squares",
        decomposer=decomposer,
        recomposer=recomposer,
    )

    # 2. Build the problem.
    problem = Problem.create(
        description="sum_of_squares(0, 500)",
        pattern_hint="embarrassingly_parallel",
        parameters={
            "n": 500,
            "n_chunks": 6,
            "kernel": "sum_of_squares",
        },
        submitter="@test:local",
    )

    # 3. Decompose.
    chunk_specs = decomposer(problem, registry)
    plan = DecompositionPlan.create(
        problem_id=problem.problem_id,
        pattern_name="embarrassingly_parallel",
        parameterization_name="sum_of_squares",
        chunks=chunk_specs,
        seed_seed=None,
        proposer="user",
    )
    chunks = [s.to_chunk(plan_id=plan.plan_id, seed=None) for s in chunk_specs]

    # 4. Dispatch through the LocalDispatcher (subprocess-free for tests).
    dispatcher = LocalDispatcher(
        kernels={"sum_of_squares": SumOfSquaresKernel()},
        leaf_node_id="@test-leaf:local",
    )
    results: list[ChunkResult] = dispatcher.dispatch_all(chunks)
    assert len(results) == len(chunks)

    # 5. Recompose.
    aggregated = aggregate_results(plan, chunks, results, recomposer)
    assert aggregated.payload["sum"] == _ground_truth(500)


def test_e2e_with_subprocess_dispatcher(tmp_path: Path):
    """Same flow but going through the SubprocessDispatcher backed by the
    ``infra.tasks`` persistent runner. Each chunk becomes a task; we
    poll until completion, then collect the JSON-serialized output
    artifacts.

    Skipped if the infra.tasks dependency cannot be loaded (CI sandbox
    without the platform fully installed)."""
    pytest.importorskip("axiom.infra.tasks")

    from axiom.compute_decomposition.runner import SubprocessDispatcher

    registry = PatternRegistry.with_builtins()
    decomposer = EmbarrassinglyParallelDecomposer()
    recomposer = SumRecomposer()
    registry.register_parameterization(
        pattern_name="embarrassingly_parallel",
        parameterization_name="sum_of_squares",
        decomposer=decomposer,
        recomposer=recomposer,
    )

    problem = Problem.create(
        description="sum_of_squares(0, 200)",
        pattern_hint="embarrassingly_parallel",
        parameters={"n": 200, "n_chunks": 4, "kernel": "sum_of_squares"},
        submitter="@test:local",
    )
    chunk_specs = decomposer(problem, registry)
    plan = DecompositionPlan.create(
        problem_id=problem.problem_id,
        pattern_name="embarrassingly_parallel",
        parameterization_name="sum_of_squares",
        chunks=chunk_specs,
        seed_seed=None,
        proposer="user",
    )
    chunks = [s.to_chunk(plan_id=plan.plan_id, seed=None) for s in chunk_specs]

    dispatcher = SubprocessDispatcher(
        leaf_node_id="@test-leaf:local",
        principal="@test:local",
        state_dir=tmp_path / "axi_state",
        kernel_name="sum_of_squares",
        timeout_seconds=20.0,
    )
    results = dispatcher.dispatch_all(chunks)
    aggregated = aggregate_results(plan, chunks, results, recomposer)
    assert aggregated.payload["sum"] == _ground_truth(200)
