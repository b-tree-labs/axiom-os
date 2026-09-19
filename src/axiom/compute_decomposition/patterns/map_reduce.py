# Copyright (c) 2026 B-Tree Ventures, LLC
# SPDX-License-Identifier: Apache-2.0

"""``map_reduce`` pattern — Phase C stub."""

from __future__ import annotations

from axiom.compute_decomposition.registry import _map_reduce_invariants


def canonical_invariants():
    return _map_reduce_invariants()


__all__ = ["canonical_invariants"]
