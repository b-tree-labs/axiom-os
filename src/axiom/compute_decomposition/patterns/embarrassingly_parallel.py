# Copyright (c) 2026 B-Tree Ventures, LLC
# SPDX-License-Identifier: Apache-2.0

"""``embarrassingly_parallel`` pattern — Phase A real implementation.

The pattern + decomposer + recomposer here are concrete and tested.
The kernel is a stub: a deterministic ``sum_of_squares`` function
that lets us exercise the end-to-end pipeline (decompose -> per-leaf
execute -> recompose) against a closed-form ground truth. Real
domain kernels (e.g. a consumer's stochastic transport batches) plug
in via the same shape: a callable that takes a parameters dict and
returns a JSON-serializable output dict.

Decomposer behavior:
- Split the integer range [0, n) into ``n_chunks`` near-equal blocks
  (the last block absorbs any remainder).
- Each ChunkSpec carries ``parameters = {"range_lo", "range_hi",
  "kernel"}`` plus ``trait = DETERMINISTIC``.

Recomposer behavior:
- Sort results by ``sequence_index`` (sum is commutative but the
  registered accumulator API takes the ordered ChunkSpec list as
  the canonical ordering, which makes test failures readable).
- Sum the per-chunk ``sum`` outputs and emit ``{"sum": total,
  "n_chunks_combined": k}``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable

from axiom.compute_decomposition.registry import (
    InvariantStatement,
    _embarrassingly_parallel_invariants,
)
from axiom.compute_decomposition.types import (
    ChunkResult,
    ChunkSpec,
    ContentRef,
    DecompositionPlan,
    Problem,
    Trait,
)


__all__ = [
    "EmbarrassinglyParallelDecomposer",
    "SumOfSquaresKernel",
    "SumAccumulator",
    "SumRecomposer",
    "canonical_invariants",
]


def canonical_invariants() -> list[InvariantStatement]:
    return _embarrassingly_parallel_invariants()


# ---------------------------------------------------------------------------
# Kernel (stub deterministic function)
# ---------------------------------------------------------------------------


class SumOfSquaresKernel:
    """Deterministic kernel: returns sum(i*i for i in [range_lo, range_hi))."""

    name = "sum_of_squares"

    def run(self, params: dict[str, Any]) -> dict[str, Any]:
        lo = int(params["range_lo"])
        hi = int(params["range_hi"])
        if hi < lo:
            raise ValueError(f"range_hi ({hi}) < range_lo ({lo})")
        total = 0
        for i in range(lo, hi):
            total += i * i
        return {"sum": total, "range_lo": lo, "range_hi": hi}


# ---------------------------------------------------------------------------
# Accumulator
# ---------------------------------------------------------------------------


class SumAccumulator:
    """Pure: combine([{"sum": x_i}, ...]) -> {"sum": sum(x_i)}."""

    name = "sum"

    def combine(self, payloads: Iterable[dict[str, Any]]) -> dict[str, Any]:
        return {"sum": sum(p["sum"] for p in payloads)}


# ---------------------------------------------------------------------------
# Decomposer
# ---------------------------------------------------------------------------


@dataclass
class EmbarrassinglyParallelDecomposer:
    """Stateless. Reads ``problem.parameters['n', 'n_chunks', 'kernel']``."""

    def __call__(self, problem: Problem, registry: Any) -> list[ChunkSpec]:
        del registry  # unused in Phase A
        params = problem.parameters
        n = int(params["n"])
        n_chunks = int(params.get("n_chunks", 1))
        kernel_name = str(params.get("kernel", "sum_of_squares"))
        if n_chunks <= 0:
            raise ValueError(f"n_chunks must be positive; got {n_chunks}")
        if n < 0:
            raise ValueError(f"n must be non-negative; got {n}")

        block = n // n_chunks
        remainder = n - (block * n_chunks)
        specs: list[ChunkSpec] = []
        cursor = 0
        for i in range(n_chunks):
            # Last block absorbs the remainder so coverage is exact.
            extra = remainder if i == (n_chunks - 1) else 0
            lo = cursor
            hi = cursor + block + extra
            cursor = hi
            specs.append(ChunkSpec(
                sequence_index=i,
                trait=Trait.DETERMINISTIC,
                parameters={
                    "range_lo": lo,
                    "range_hi": hi,
                    "kernel": kernel_name,
                },
                adapter_language="python",
                expected_runtime_s=max(0.001, (hi - lo) * 1e-6),
            ))
        return specs


# ---------------------------------------------------------------------------
# Recomposer
# ---------------------------------------------------------------------------


@dataclass
class SumRecomposer:
    """Aggregates per-chunk sums into a single ``{"sum": total}`` payload.

    Round-trip property: for any decomposition the recomposer's output
    equals the closed-form sum-of-squares ground truth.
    """

    accumulator_name: str = "sum"

    def aggregate(
        self,
        chunks: list[ChunkSpec] | tuple[ChunkSpec, ...] | list[Any],
        results: list[ChunkResult],
    ) -> dict[str, Any]:
        if len(results) != len(chunks):
            raise ValueError(
                f"missing chunk results: expected {len(chunks)}, got {len(results)}"
            )
        # Order by sequence_index for readable test failures.
        ordered = sorted(results, key=lambda r: _seq_for_result(r, chunks))
        accum = SumAccumulator()
        return {
            **accum.combine(r.payload for r in ordered),
            "n_chunks_combined": len(ordered),
        }

    # The Recomposer Protocol signature.
    def __call__(
        self, plan: DecompositionPlan, results: list[ChunkResult],
    ) -> ContentRef:
        payload = self.aggregate(list(plan.chunks), results)
        return ContentRef.from_payload(payload)


def _seq_for_result(result: ChunkResult, chunks) -> int:
    # Map a result back to its chunk_id -> sequence_index.
    for c in chunks:
        cid = getattr(c, "chunk_id", None)
        if cid is not None and cid == result.chunk_id:
            return c.sequence_index
        # ChunkSpec doesn't carry chunk_id; fall through to a stable
        # sort by chunk_id when both are ChunkSpec lists.
    return hash(result.chunk_id) & 0xFFFFFFFF
