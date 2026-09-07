#!/usr/bin/env python3
# Copyright (c) 2026 B-Tree Ventures, LLC
# SPDX-License-Identifier: Apache-2.0

"""Demo: end-to-end embarrassingly_parallel round-trip via the
compute-decomposition primitive (Phase A scaffold).

This script exercises the full Phase A pipeline locally — no remote node,
no federation gossip, no LLM. The point is to make the pattern +
trait + decomposer + dispatcher + recomposer wiring visible in one
~200-line script that anyone can run::

    python scripts/demo-fmp-phase-a-scaffold.py
    python scripts/demo-fmp-phase-a-scaffold.py --n 5000 --chunks 9
    python scripts/demo-fmp-phase-a-scaffold.py --subprocess

Expected output (default args)::

    [decompose] plan plan-XXXXXXXX
                pattern=embarrassingly_parallel param=sum_of_squares
                chunks=6
    [dispatch ] running 6 chunks via LocalDispatcher
                chunk-0000 ... done in   0 ms (sum=...)
                ...
    [aggregate] payload {'sum': 332833500, 'n_chunks_combined': 6}
                hash    sha256:....
                ground  332833500
    [verify   ] aggregate matches closed-form ground truth.

The ``--subprocess`` flag swaps in the SubprocessDispatcher backed by
``infra.tasks`` to demonstrate the same flow under the per-leaf
runner contract.
"""

from __future__ import annotations

import argparse
import sys
import textwrap
from pathlib import Path

# When run from a source checkout, prefer this worktree's src/.
_REPO_SRC = Path(__file__).resolve().parents[1] / "src"
if _REPO_SRC.is_dir():
    sys.path.insert(0, str(_REPO_SRC))

from axiom.compute_decomposition import (  # noqa: E402
    DecompositionPlan,
    LocalDispatcher,
    PatternRegistry,
    Problem,
    SubprocessDispatcher,
    aggregate_results,
    register_pattern_parameterization,
)
from axiom.compute_decomposition.patterns.embarrassingly_parallel import (  # noqa: E402
    EmbarrassinglyParallelDecomposer,
    SumOfSquaresKernel,
    SumRecomposer,
)


def _ground_truth(n: int) -> int:
    return n * (n - 1) * (2 * n - 1) // 6


def _print_section(title: str) -> None:
    print()
    print(f"--- {title} ".ljust(72, "-"))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--n", type=int, default=1000,
                    help="Upper bound (exclusive) of the integer range "
                         "(default: 1000)")
    ap.add_argument("--chunks", type=int, default=6,
                    help="Number of decomposition chunks (default: 6)")
    ap.add_argument("--subprocess", action="store_true",
                    help="Use the SubprocessDispatcher (infra.tasks-backed) "
                         "instead of the in-process LocalDispatcher")
    ap.add_argument("--state-dir", default=None,
                    help="State dir for SubprocessDispatcher (default: a tmp "
                         "dir under /tmp)")
    args = ap.parse_args()

    # ----- 1. Register the parameterization -------------------------------
    _print_section("setup")
    registry = PatternRegistry.with_builtins()
    decomposer = EmbarrassinglyParallelDecomposer()
    recomposer = SumRecomposer()
    receipt = register_pattern_parameterization(
        pattern_name="embarrassingly_parallel",
        parameterization_name="sum_of_squares",
        decomposer=decomposer,
        recomposer=recomposer,
        registry=registry,
    )
    print(f"registered {receipt.parameterization_name!r} on pattern "
          f"{receipt.pattern_name!r} (rev={receipt.revision})")

    # ----- 2. Build a Problem manifest in-memory ---------------------------
    problem = Problem.create(
        description=f"sum_of_squares(0, {args.n})",
        pattern_hint="embarrassingly_parallel",
        parameters={
            "n": args.n,
            "n_chunks": args.chunks,
            "kernel": "sum_of_squares",
        },
        submitter="@demo:local",
    )
    print(f"problem  {problem.problem_id}")
    print(f"  desc:  {problem.description}")
    print(f"  param: {problem.parameters}")

    # ----- 3. Decompose ----------------------------------------------------
    _print_section("decompose")
    chunk_specs = decomposer(problem, registry)
    plan = DecompositionPlan.create(
        problem_id=problem.problem_id,
        pattern_name="embarrassingly_parallel",
        parameterization_name="sum_of_squares",
        chunks=chunk_specs,
        seed_seed=None,
        proposer="user",
    )
    print(f"plan {plan.plan_id}  pattern={plan.pattern_name} "
          f"param={plan.parameterization_name}  chunks={len(plan.chunks)}")
    for cs in plan.chunks:
        rng = (cs.parameters["range_lo"], cs.parameters["range_hi"])
        print(f"  - seq={cs.sequence_index} range={rng} trait={cs.trait.value} "
              f"adapter={cs.adapter_language}")

    # Materialize ChunkSpec -> Chunk (binds plan_id + chunk_id + cache_key)
    chunks = [cs.to_chunk(plan_id=plan.plan_id, seed=None) for cs in chunk_specs]

    # ----- 4. Dispatch -----------------------------------------------------
    _print_section("dispatch")
    if args.subprocess:
        from tempfile import mkdtemp
        state_dir = Path(args.state_dir) if args.state_dir else Path(mkdtemp(prefix="axi-fmp-demo-"))
        print(f"using SubprocessDispatcher  state_dir={state_dir}")
        dispatcher = SubprocessDispatcher(
            leaf_node_id="@demo-leaf:local",
            principal="@demo:local",
            state_dir=state_dir,
            kernel_name="sum_of_squares",
        )
    else:
        print("using LocalDispatcher (in-process)")
        dispatcher = LocalDispatcher(
            kernels={"sum_of_squares": SumOfSquaresKernel()},
            leaf_node_id="@demo-leaf:local",
        )

    print(f"dispatching {len(chunks)} chunks ...")
    results = dispatcher.dispatch_all(chunks)
    for r in results:
        print(f"  - {r.chunk_id}  elapsed_ms={r.elapsed_ms:>4}  "
              f"sum={r.payload['sum']}  hash={r.output.content_hash[:24]}...")

    # ----- 5. Recompose ----------------------------------------------------
    _print_section("aggregate")
    aggregated = aggregate_results(plan, chunks, results, recomposer)
    truth = _ground_truth(args.n)
    print(f"plan_id            = {aggregated.plan_id}")
    print(f"pattern            = {aggregated.pattern_name} / "
          f"{aggregated.parameterization_name}")
    print(f"contributing       = {aggregated.contributing_result_count}")
    print(f"payload            = {aggregated.payload}")
    print(f"output content_hash= {aggregated.output_ref.content_hash}")
    print(f"ground truth       = {truth}")

    # ----- 6. Verify -------------------------------------------------------
    _print_section("verify")
    if aggregated.payload["sum"] == truth:
        print("PASS  aggregate matches closed-form ground truth "
              "(N*(N-1)*(2N-1)/6).")
        return 0
    else:
        print(f"FAIL  aggregate {aggregated.payload['sum']} != ground {truth}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
