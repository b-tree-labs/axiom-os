# Copyright (c) 2026 B-Tree Ventures, LLC
# SPDX-License-Identifier: Apache-2.0

"""``spatial_domain`` pattern — Phase C scope.

Phase A: stub. The canonical-invariant list is wired through the
registry; concrete decomposer + recomposer + halo-merge logic land
in Phase C per spec §7.2.
"""

from __future__ import annotations

from axiom.compute_decomposition.registry import _spatial_domain_invariants


def canonical_invariants():
    return _spatial_domain_invariants()


__all__ = ["canonical_invariants"]
