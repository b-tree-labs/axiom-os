# Copyright (c) 2026 B-Tree Ventures, LLC
# SPDX-License-Identifier: Apache-2.0

"""Monte Carlo darts kernel + recomposer (the high-dim integral toy).

A second concrete kernel for the ``embarrassingly_parallel`` pattern,
complementing ``SumOfSquaresKernel``. Where sum-of-squares is the
deterministic stand-in, this one is the *stochastic* stand-in:
estimate pi by counting how many uniformly-sampled points in
``[-1, 1]^2`` land inside the unit disc.

The kernel + recomposer here are the toy. The narrative makes the
generalisation explicit: the same shape (independent random samples,
sum-and-divide aggregation) is what high-dimensional Monte Carlo
integration looks like — it's just that for pi the answer is also in
the table, so we can audit empirically.

Determinism via seeded randomness:

- Each chunk is seeded by ``hash(chunk_id)`` (mod 2**32). Same
  chunk_id + same range → bit-identical hits. Different chunk_ids →
  independent streams (collision probability negligible at our
  sample sizes).

This is what lets us verify the audit-grade "same routing → same
answer" property even with a stochastic kernel: the receipt records
the chunk_id assignments, and replaying with the same chunk_ids
produces the same hits per chunk.

Public surface (registered against ``embarrassingly_parallel``):

- ``MonteCarloDecomposer`` — splits N samples into n_chunks blocks.
- ``MonteCarloDartsKernel`` — runs one chunk's samples.
- ``MonteCarloPiRecomposer`` — sums hits + trials; emits pi_estimate.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np

from axiom.compute_decomposition.types import (
    ChunkResult,
    ChunkSpec,
    ContentRef,
    DecompositionPlan,
    Trait,
)


__all__ = [
    "MonteCarloDartsKernel",
    "MonteCarloDecomposer",
    "MonteCarloPiRecomposer",
]


# ---------------------------------------------------------------------------
# Kernel
# ---------------------------------------------------------------------------


def _seed_from_chunk_id(chunk_id: str) -> int:
    """Stable 32-bit seed from a chunk_id string. Independent of
    Python's hash randomization (which would break determinism across
    interpreter restarts)."""
    # Use a stable byte hash (sha-derived) rather than ``hash()``.
    import hashlib
    digest = hashlib.sha256(chunk_id.encode("utf-8")).digest()
    return int.from_bytes(digest[:4], "big")


class MonteCarloDartsKernel:
    """Stochastic kernel: count points in the unit disc.

    Reads ``params``:

    - ``chunk_id`` (str) — used as the deterministic RNG seed.
    - ``range_lo`` / ``range_hi`` (int) — the chunk's slice of the
      total sample stream. ``count = range_hi - range_lo``.

    Returns ``{"hits": int, "trials": int, "range_lo": int,
    "range_hi": int}``.

    The chunk's per-sample work is a tiny vectorised numpy op — this
    is the cheap-kernel regime where federation overhead is the
    dominant cost.
    """

    name = "monte_carlo_pi"

    def run(self, params: dict[str, Any]) -> dict[str, Any]:
        chunk_id = params.get("chunk_id")
        if not chunk_id:
            raise ValueError(
                "MonteCarloDartsKernel requires 'chunk_id' in params "
                "for deterministic seeding"
            )
        lo = int(params["range_lo"])
        hi = int(params["range_hi"])
        if hi < lo:
            raise ValueError(f"range_hi ({hi}) < range_lo ({lo})")
        trials = hi - lo

        # Optional sleep-per-sample knob lets the demo simulate
        # expensive kernels (mcmc-fake, openmc-fake) without writing
        # a separate Kernel class for each. Default 0.0 → no sleep.
        sleep_per_sample = float(params.get("sleep_per_sample_s", 0.0))

        rng = np.random.default_rng(seed=_seed_from_chunk_id(chunk_id))
        # Vectorised: draw all samples at once.
        xs = rng.uniform(-1.0, 1.0, size=trials)
        ys = rng.uniform(-1.0, 1.0, size=trials)
        inside = (xs * xs + ys * ys) <= 1.0
        hits = int(inside.sum())

        if sleep_per_sample > 0.0 and trials > 0:
            import time
            time.sleep(sleep_per_sample * trials)

        return {
            "hits": hits,
            "trials": trials,
            "range_lo": lo,
            "range_hi": hi,
        }


# ---------------------------------------------------------------------------
# Decomposer
# ---------------------------------------------------------------------------


@dataclass
class MonteCarloDecomposer:
    """Stateless. Reads ``problem.parameters['n', 'n_chunks',
    'kernel']``.

    Tiles ``[0, n)`` into ``n_chunks`` near-equal blocks; the last
    block absorbs the remainder so coverage is exact. Each block's
    expected runtime is dominated by the per-sample cost (which
    matters for routing — see ``compute_capacity_hint`` in
    ``select_peers``)."""

    def __call__(self, problem, registry: Any) -> list[ChunkSpec]:
        del registry  # unused
        params = problem.parameters
        n = int(params["n"])
        n_chunks = int(params.get("n_chunks", 1))
        kernel_name = str(params.get("kernel", "monte_carlo_pi"))
        sleep_per_sample = float(params.get("sleep_per_sample_s", 0.0))
        if n_chunks <= 0:
            raise ValueError(f"n_chunks must be positive; got {n_chunks}")
        if n < 0:
            raise ValueError(f"n must be non-negative; got {n}")

        block = n // n_chunks
        remainder = n - (block * n_chunks)
        specs: list[ChunkSpec] = []
        cursor = 0
        for i in range(n_chunks):
            extra = remainder if i == (n_chunks - 1) else 0
            lo = cursor
            hi = cursor + block + extra
            cursor = hi
            chunk_params: dict[str, Any] = {
                "range_lo": lo,
                "range_hi": hi,
                "kernel": kernel_name,
            }
            if sleep_per_sample > 0.0:
                chunk_params["sleep_per_sample_s"] = sleep_per_sample
            specs.append(ChunkSpec(
                sequence_index=i,
                trait=Trait.STOCHASTIC,
                parameters=chunk_params,
                adapter_language="python",
                expected_runtime_s=max(
                    0.001,
                    (hi - lo) * (1e-7 + sleep_per_sample),
                ),
            ))
        return specs


# ---------------------------------------------------------------------------
# Recomposer
# ---------------------------------------------------------------------------


@dataclass
class MonteCarloPiRecomposer:
    """Sums per-chunk hits + trials → ``pi_estimate = 4 * hits / trials``.

    The aggregation is associative + commutative (sum), so the
    embarrassingly_parallel invariant holds.
    """

    accumulator_name: str = "tally_pool"

    def aggregate(
        self,
        chunks: list[ChunkSpec] | tuple[ChunkSpec, ...] | list[Any],
        results: list[ChunkResult],
    ) -> dict[str, Any]:
        if chunks and len(results) != len(chunks):
            raise ValueError(
                f"missing chunk results: expected {len(chunks)}, got "
                f"{len(results)}"
            )
        total_hits = sum(int(r.payload["hits"]) for r in results)
        total_trials = sum(int(r.payload["trials"]) for r in results)
        pi_estimate = (4.0 * total_hits / total_trials) if total_trials else 0.0
        return {
            "hits": total_hits,
            "trials": total_trials,
            "pi_estimate": pi_estimate,
            "n_chunks_combined": len(results),
        }

    def __call__(
        self, plan: DecompositionPlan, results: list[ChunkResult],
    ) -> ContentRef:
        payload = self.aggregate(list(plan.chunks), results)
        return ContentRef.from_payload(payload)
