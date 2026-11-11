# Copyright (c) 2026 B-Tree Ventures, LLC
# SPDX-License-Identifier: Apache-2.0

"""``matrix_block`` pattern — Phase C stub."""

from __future__ import annotations

from axiom.compute_decomposition.registry import _matrix_block_invariants


def canonical_invariants():
    return _matrix_block_invariants()


__all__ = ["canonical_invariants"]
