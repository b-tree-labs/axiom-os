# Copyright (c) 2026 B-Tree Ventures, LLC
# SPDX-License-Identifier: Apache-2.0

"""``temporal_stepping`` pattern — Phase C stub. See spec §7.3."""

from __future__ import annotations

from axiom.compute_decomposition.registry import _temporal_stepping_invariants


def canonical_invariants():
    return _temporal_stepping_invariants()


__all__ = ["canonical_invariants"]
