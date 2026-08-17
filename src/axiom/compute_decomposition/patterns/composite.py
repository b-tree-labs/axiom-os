# Copyright (c) 2026 B-Tree Ventures, LLC
# SPDX-License-Identifier: Apache-2.0

"""``composite`` pattern — Phase B stub.

Composes two registered patterns (outer + inner). The recomposer is
``outer_recomposer(inner_recomposer(per_outer_chunk))``. Real
implementation lands in Phase B per spec §7.4.
"""

from __future__ import annotations

from axiom.compute_decomposition.registry import _composite_invariants


def canonical_invariants():
    return _composite_invariants()


__all__ = ["canonical_invariants"]
