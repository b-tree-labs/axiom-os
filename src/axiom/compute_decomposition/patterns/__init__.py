# Copyright (c) 2026 B-Tree Ventures, LLC
# SPDX-License-Identifier: Apache-2.0

"""Built-in pattern modules.

Phase A ships a real ``embarrassingly_parallel`` pattern with a
deterministic kernel + decomposer + recomposer. The other five
patterns from the closed vocabulary live as stubs (Phase B/C scope)
so the registry's canonical-invariant lists already point somewhere
real."""

from .embarrassingly_parallel import (
    EmbarrassinglyParallelDecomposer,
    SumOfSquaresKernel,
    SumAccumulator,
    SumRecomposer,
    canonical_invariants as embarrassingly_parallel_invariants,
)

__all__ = [
    "EmbarrassinglyParallelDecomposer",
    "SumOfSquaresKernel",
    "SumAccumulator",
    "SumRecomposer",
    "embarrassingly_parallel_invariants",
]
