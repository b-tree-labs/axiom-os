# Copyright (c) 2026 B-Tree Ventures, LLC
# SPDX-License-Identifier: Apache-2.0

"""Tests for the closed PatternRegistry per ADR-040 D2 + spec §3.2.

Properties tested:
- The closed pattern vocabulary is exactly the six names declared in
  the spec (no surprises, no silent additions).
- Each built-in pattern carries a non-empty canonical invariant set.
- Parameterizations register against a closed pattern; unknown
  patterns raise.
- Re-registering the same (pattern, parameterization) without
  ``replace=True`` raises a conflict.
- Re-registering with ``replace=True`` succeeds and bumps the
  registration receipt's revision counter.
- Lookup of a registered parameterization round-trips the
  decomposer + recomposer pair.
"""

from __future__ import annotations

import pytest

from axiom.compute_decomposition.registry import (
    BUILTIN_PATTERN_NAMES,
    PatternRegistry,
    UnknownPatternError,
    PatternConflictError,
)
from axiom.compute_decomposition.types import (
    ContentRef,
)


# Closed pattern vocabulary per ADR-040 D2.
EXPECTED_PATTERNS: frozenset[str] = frozenset({
    "embarrassingly_parallel",
    "spatial_domain",
    "temporal_stepping",
    "matrix_block",
    "map_reduce",
    "composite",
})


def test_builtin_pattern_vocabulary_is_closed_and_exact():
    """The six pattern names are the entire closed set; no more, no less."""
    assert set(BUILTIN_PATTERN_NAMES) == EXPECTED_PATTERNS


def test_each_builtin_pattern_has_canonical_invariants():
    reg = PatternRegistry.with_builtins()
    for name in BUILTIN_PATTERN_NAMES:
        invariants = reg.canonical_invariants(name)
        assert invariants, f"pattern {name!r} declares no canonical invariants"


def test_register_against_unknown_pattern_raises():
    reg = PatternRegistry.with_builtins()

    def _decomp(problem, registry):  # noqa: ARG001
        return []

    def _recomp(plan, results):  # noqa: ARG001
        return ContentRef(content_hash="x", uri="axiom://artifact/x", bytes=0,
                          media_type="application/json")

    with pytest.raises(UnknownPatternError):
        reg.register_parameterization(
            pattern_name="not_a_real_pattern",
            parameterization_name="x",
            decomposer=_decomp,
            recomposer=_recomp,
        )


def test_register_then_lookup_round_trip():
    reg = PatternRegistry.with_builtins()

    def _decomp(problem, registry):  # noqa: ARG001
        return []

    def _recomp(plan, results):  # noqa: ARG001
        return ContentRef(content_hash="x", uri="axiom://artifact/x", bytes=0,
                          media_type="application/json")

    receipt = reg.register_parameterization(
        pattern_name="embarrassingly_parallel",
        parameterization_name="trivial_test",
        decomposer=_decomp,
        recomposer=_recomp,
    )

    assert receipt.pattern_name == "embarrassingly_parallel"
    assert receipt.parameterization_name == "trivial_test"
    assert receipt.revision == 1

    looked_up = reg.get_parameterization(
        "embarrassingly_parallel", "trivial_test",
    )
    assert looked_up.decomposer is _decomp
    assert looked_up.recomposer is _recomp


def test_re_register_without_replace_raises_conflict():
    reg = PatternRegistry.with_builtins()

    def _decomp(problem, registry):  # noqa: ARG001
        return []

    def _recomp(plan, results):  # noqa: ARG001
        return ContentRef(content_hash="x", uri="axiom://artifact/x", bytes=0,
                          media_type="application/json")

    reg.register_parameterization(
        pattern_name="embarrassingly_parallel",
        parameterization_name="dup",
        decomposer=_decomp,
        recomposer=_recomp,
    )

    with pytest.raises(PatternConflictError):
        reg.register_parameterization(
            pattern_name="embarrassingly_parallel",
            parameterization_name="dup",
            decomposer=_decomp,
            recomposer=_recomp,
        )


def test_re_register_with_replace_bumps_revision():
    reg = PatternRegistry.with_builtins()

    def _decomp(problem, registry):  # noqa: ARG001
        return []

    def _recomp(plan, results):  # noqa: ARG001
        return ContentRef(content_hash="x", uri="axiom://artifact/x", bytes=0,
                          media_type="application/json")

    reg.register_parameterization(
        pattern_name="embarrassingly_parallel",
        parameterization_name="upgradable",
        decomposer=_decomp,
        recomposer=_recomp,
    )
    receipt2 = reg.register_parameterization(
        pattern_name="embarrassingly_parallel",
        parameterization_name="upgradable",
        decomposer=_decomp,
        recomposer=_recomp,
        replace=True,
    )
    assert receipt2.revision == 2


def test_list_parameterizations_for_pattern():
    reg = PatternRegistry.with_builtins()

    def _decomp(problem, registry):  # noqa: ARG001
        return []

    def _recomp(plan, results):  # noqa: ARG001
        return ContentRef(content_hash="x", uri="axiom://artifact/x", bytes=0,
                          media_type="application/json")

    reg.register_parameterization(
        pattern_name="embarrassingly_parallel",
        parameterization_name="alpha",
        decomposer=_decomp,
        recomposer=_recomp,
    )
    reg.register_parameterization(
        pattern_name="embarrassingly_parallel",
        parameterization_name="beta",
        decomposer=_decomp,
        recomposer=_recomp,
    )
    names = reg.list_parameterizations("embarrassingly_parallel")
    assert set(names) == {"alpha", "beta"}
