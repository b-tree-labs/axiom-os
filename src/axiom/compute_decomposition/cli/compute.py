# Copyright (c) 2026 B-Tree Ventures, LLC
# SPDX-License-Identifier: Apache-2.0

"""``axi compute`` CLI surface.

Phase A scope (per spec §12, scoped down):

    axi compute decompose <problem.toml>    # produce DecompositionPlan; show summary
    axi compute dispatch  <problem.toml>    # decompose + run all chunks via LocalDispatcher
    axi compute aggregate <problem.toml>    # full pipeline: decompose + dispatch + aggregate
    axi compute peers                       # placeholder (Phase B: federation directory scan)
    axi compute offer status                # placeholder

The Phase A pipeline runs locally (no federation gossip yet). The
``axi compute`` namespace registered through the AEOS extension
mechanism in a later phase; for now this module exposes a
free-standing ``main()`` callable from
``python -m axiom.compute_decomposition.cli.compute``.
"""

from __future__ import annotations

import argparse
import json
import sys
import tomllib
from pathlib import Path

from axiom.compute_decomposition.aggregator import aggregate_results
from axiom.compute_decomposition.patterns.embarrassingly_parallel import (
    EmbarrassinglyParallelDecomposer,
    SumOfSquaresKernel,
    SumRecomposer,
)
from axiom.compute_decomposition.registry import PatternRegistry
from axiom.compute_decomposition.runner import LocalDispatcher
from axiom.compute_decomposition.types import (
    DecompositionPlan,
    Problem,
)


__all__ = ["main", "build_parser"]


# ---------------------------------------------------------------------------
# Problem loader
# ---------------------------------------------------------------------------


def _load_problem(path: Path) -> Problem:
    if not path.exists():
        raise FileNotFoundError(f"problem file not found: {path}")
    with path.open("rb") as f:
        data = tomllib.load(f)
    prob = data.get("problem", data)
    return Problem.create(
        description=prob["description"],
        pattern_hint=prob.get("pattern_hint"),
        parameters=prob.get("parameters", {}),
        submitter=prob.get("submitter", "@anon:local"),
        classification=prob.get("classification", "public"),
        visibility=prob.get("visibility", "cohort"),
    )


def _seed_default_registry() -> PatternRegistry:
    reg = PatternRegistry.with_builtins()
    reg.register_parameterization(
        pattern_name="embarrassingly_parallel",
        parameterization_name="sum_of_squares",
        decomposer=EmbarrassinglyParallelDecomposer(),
        recomposer=SumRecomposer(),
    )
    return reg


# ---------------------------------------------------------------------------
# Verbs
# ---------------------------------------------------------------------------


def _do_decompose(args: argparse.Namespace) -> int:
    problem = _load_problem(Path(args.problem))
    reg = _seed_default_registry()
    parameterization = args.parameterization or _resolve_parameterization(problem, reg)
    entry = reg.get_parameterization(problem.pattern_hint or "embarrassingly_parallel",
                                     parameterization)
    chunk_specs = entry.decomposer(problem, reg)
    plan = DecompositionPlan.create(
        problem_id=problem.problem_id,
        pattern_name=entry.pattern_name,
        parameterization_name=entry.parameterization_name,
        chunks=chunk_specs,
        seed_seed=None,
        proposer="user",
    )
    out = {
        "problem_id": problem.problem_id,
        "plan_id": plan.plan_id,
        "pattern": plan.pattern_name,
        "parameterization": plan.parameterization_name,
        "chunk_count": len(plan.chunks),
        "chunks": [
            {
                "sequence_index": c.sequence_index,
                "trait": c.trait.value,
                "parameters": c.parameters,
                "expected_runtime_s": c.expected_runtime_s,
            }
            for c in plan.chunks
        ],
    }
    if args.json:
        print(json.dumps(out, indent=2))
    else:
        print(f"Problem:  {problem.description}")
        print(f"  problem_id      = {problem.problem_id}")
        print(f"  plan_id         = {plan.plan_id}")
        print(f"  pattern         = {plan.pattern_name}")
        print(f"  parameterization= {plan.parameterization_name}")
        print(f"  chunks          = {len(plan.chunks)}")
        for c in plan.chunks:
            print(f"   - seq={c.sequence_index} trait={c.trait.value} "
                  f"params={c.parameters}")
    return 0


def _do_dispatch(args: argparse.Namespace) -> int:
    problem = _load_problem(Path(args.problem))
    reg = _seed_default_registry()
    parameterization = args.parameterization or _resolve_parameterization(problem, reg)
    entry = reg.get_parameterization(problem.pattern_hint or "embarrassingly_parallel",
                                     parameterization)
    chunk_specs = entry.decomposer(problem, reg)
    plan = DecompositionPlan.create(
        problem_id=problem.problem_id,
        pattern_name=entry.pattern_name,
        parameterization_name=entry.parameterization_name,
        chunks=chunk_specs,
        seed_seed=None,
        proposer="user",
    )
    chunks = [s.to_chunk(plan_id=plan.plan_id, seed=None) for s in chunk_specs]
    dispatcher = LocalDispatcher(
        kernels={"sum_of_squares": SumOfSquaresKernel()},
        leaf_node_id="@cli-leaf:local",
    )
    results = dispatcher.dispatch_all(chunks)
    out = {
        "plan_id": plan.plan_id,
        "results": [
            {
                "chunk_id": r.chunk_id,
                "sequence_index": idx,
                "leaf_node_id": r.leaf_node_id,
                "elapsed_ms": r.elapsed_ms,
                "output_ref": r.output.content_hash,
                "payload": r.payload,
            }
            for idx, r in enumerate(results)
        ],
    }
    if args.json:
        print(json.dumps(out, indent=2))
    else:
        print(f"Dispatched {len(results)} chunks for plan {plan.plan_id}")
        for r in results:
            print(f"  chunk={r.chunk_id} elapsed_ms={r.elapsed_ms} "
                  f"hash={r.output.content_hash[:16]}...")
    return 0


def _do_aggregate(args: argparse.Namespace) -> int:
    problem = _load_problem(Path(args.problem))
    reg = _seed_default_registry()
    parameterization = args.parameterization or _resolve_parameterization(problem, reg)
    entry = reg.get_parameterization(problem.pattern_hint or "embarrassingly_parallel",
                                     parameterization)
    chunk_specs = entry.decomposer(problem, reg)
    plan = DecompositionPlan.create(
        problem_id=problem.problem_id,
        pattern_name=entry.pattern_name,
        parameterization_name=entry.parameterization_name,
        chunks=chunk_specs,
        seed_seed=None,
        proposer="user",
    )
    chunks = [s.to_chunk(plan_id=plan.plan_id, seed=None) for s in chunk_specs]
    dispatcher = LocalDispatcher(
        kernels={"sum_of_squares": SumOfSquaresKernel()},
        leaf_node_id="@cli-leaf:local",
    )
    results = dispatcher.dispatch_all(chunks)
    aggregated = aggregate_results(plan, chunks, results, entry.recomposer)
    out = {
        "plan_id": plan.plan_id,
        "pattern": plan.pattern_name,
        "parameterization": plan.parameterization_name,
        "aggregate_payload": aggregated.payload,
        "aggregate_hash": aggregated.output_ref.content_hash,
        "contributing_chunks": aggregated.contributing_result_count,
    }
    if args.json:
        print(json.dumps(out, indent=2))
    else:
        print(f"Aggregated artifact for plan {plan.plan_id}")
        print(f"  payload = {aggregated.payload}")
        print(f"  hash    = {aggregated.output_ref.content_hash}")
    return 0


def _do_peers(args: argparse.Namespace) -> int:
    print("Phase A: federation gossip not yet wired; this command lists local node only.")
    print("(Wire in COMPUTE_OFFER scan post-Prague per spec §5.1.)")
    return 0


def _do_offer_status(args: argparse.Namespace) -> int:
    print("Phase A: COMPUTE_OFFER status reporting not yet wired.")
    print("(Schemas defined in axiom.compute_decomposition.directory_records.)")
    return 0


def _resolve_parameterization(problem: Problem, reg: PatternRegistry) -> str:
    pattern = problem.pattern_hint or "embarrassingly_parallel"
    candidates = reg.list_parameterizations(pattern)
    if not candidates:
        raise SystemExit(
            f"no parameterizations registered for pattern {pattern!r}; "
            f"register one or pass --parameterization explicitly"
        )
    if len(candidates) > 1:
        raise SystemExit(
            f"multiple parameterizations registered for pattern {pattern!r}: "
            f"{candidates}; pick one via --parameterization"
        )
    return candidates[0]


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="axi compute",
        description="Decompose, dispatch, and aggregate compute work.",
    )
    sub = parser.add_subparsers(dest="verb", required=True)

    p_decomp = sub.add_parser("decompose", help="Produce a DecompositionPlan; do not run.")
    p_decomp.add_argument("problem", help="Path to a problem.toml manifest")
    p_decomp.add_argument("--parameterization", default=None,
                           help="Parameterization name (defaults to the sole "
                                "registered one for the pattern)")
    p_decomp.add_argument("--json", action="store_true", help="Machine-readable output")
    p_decomp.set_defaults(func=_do_decompose)

    p_disp = sub.add_parser("dispatch",
                             help="Decompose + run all chunks via LocalDispatcher.")
    p_disp.add_argument("problem")
    p_disp.add_argument("--parameterization", default=None)
    p_disp.add_argument("--json", action="store_true")
    p_disp.set_defaults(func=_do_dispatch)

    p_agg = sub.add_parser("aggregate",
                            help="Full pipeline: decompose + dispatch + aggregate.")
    p_agg.add_argument("problem")
    p_agg.add_argument("--parameterization", default=None)
    p_agg.add_argument("--json", action="store_true")
    p_agg.set_defaults(func=_do_aggregate)

    p_peers = sub.add_parser("peers", help="List cohort peers (Phase A: stub)")
    p_peers.set_defaults(func=_do_peers)

    offer = sub.add_parser("offer", help="Manage this node's COMPUTE_OFFER")
    offer_sub = offer.add_subparsers(dest="offer_verb", required=True)
    p_offer_status = offer_sub.add_parser("status", help="Show this node's COMPUTE_OFFER")
    p_offer_status.set_defaults(func=_do_offer_status)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
