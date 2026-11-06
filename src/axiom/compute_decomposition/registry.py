# Copyright (c) 2026 B-Tree Ventures, LLC
# SPDX-License-Identifier: Apache-2.0

"""PatternRegistry — closed pattern vocabulary + open parameterizations.

Per spec §3.2 + ADR-040 D2:

- Six built-in pattern names form the closed vocabulary.
- Each pattern carries a canonical InvariantStatement set (the
  verifier's checklist).
- Extensions register parameterizations against a built-in pattern;
  re-registration without ``replace=True`` raises a conflict.
- Lookups round-trip the (decomposer, recomposer) pair.

Phase A ships the closed registry surface + canonical invariant lists;
the verifier hooks into Phase B.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Optional

from .types import (
    Decomposer,
    InvariantStatement,
    Recomposer,
)


__all__ = [
    "BUILTIN_PATTERN_NAMES",
    "PatternRegistry",
    "RegistrationReceipt",
    "RegisteredParameterization",
    "UnknownPatternError",
    "PatternConflictError",
    "register_pattern_parameterization",
]


# Closed set of built-in pattern names (ADR-040 D2). Adding a name
# here is an ADR-level change.
BUILTIN_PATTERN_NAMES: frozenset[str] = frozenset({
    "embarrassingly_parallel",
    "spatial_domain",
    "temporal_stepping",
    "matrix_block",
    "map_reduce",
    "composite",
})


class UnknownPatternError(ValueError):
    """Raised when a parameterization registers against a name that is
    not in the closed pattern vocabulary."""


class PatternConflictError(ValueError):
    """Raised when re-registering a parameterization without ``replace=True``."""


# ---------------------------------------------------------------------------
# Canonical invariants per pattern (Phase A: declarative; Phase B wires
# the verifier callbacks).
# ---------------------------------------------------------------------------


def _embarrassingly_parallel_invariants() -> list[InvariantStatement]:
    return [
        InvariantStatement(
            name="independence",
            description="No chunk reads or writes shared mutable state.",
        ),
        InvariantStatement(
            name="accumulator",
            description="Aggregator uses a registered accumulator (sum, "
                        "weighted_sum, mean, weighted_mean, tally_pool, "
                        "concat_ordered, concat_unordered).",
        ),
        InvariantStatement(
            name="stochastic_seed_discipline",
            description="seed = f(plan.seed_seed, sequence_index[, retry_count]); "
                        "f is from a registered closed set.",
        ),
        InvariantStatement(
            name="round_trip",
            description="recomposer(decomposer(P)) matches ground truth on the "
                        "registered fixture set (bit-identical for deterministic; "
                        "within tolerance for stochastic).",
        ),
    ]


def _spatial_domain_invariants() -> list[InvariantStatement]:
    return [
        InvariantStatement(
            name="tiling",
            description="Subdomains tile the full domain; no gaps; no overlaps "
                        "except declared halos.",
        ),
        InvariantStatement(
            name="halo_width",
            description="halo_width >= stencil_radius for the declared solver order.",
        ),
        InvariantStatement(
            name="halo_merge_associative_commutative",
            description="Recomposer's halo-merge operator is associative + "
                        "commutative on the declared accumulator.",
        ),
        InvariantStatement(
            name="convergence_iterative",
            description="If the solver iterates, the outer-loop convergence "
                        "criterion is declared and met within K iterations on the "
                        "fixture set.",
        ),
    ]


def _temporal_stepping_invariants() -> list[InvariantStatement]:
    return [
        InvariantStatement(
            name="time_step_ordering",
            description="Chunks ordered by start_time; no temporal overlap.",
        ),
        InvariantStatement(
            name="coupling_boundary",
            description="At each coupling time, the registered coupling-residual "
                        "check passes.",
        ),
        InvariantStatement(
            name="recomposition",
            description="Concatenation produces a single trajectory whose state "
                        "at any t matches per-chunk endpoints to declared tolerance.",
        ),
    ]


def _matrix_block_invariants() -> list[InvariantStatement]:
    return [
        InvariantStatement(
            name="block_partition_covers_matrix",
            description="The block partition tiles the matrix; no gaps; no overlaps.",
        ),
        InvariantStatement(
            name="recomposer_dimension_check",
            description="Recomposed matrix dimensions match the input shape.",
        ),
        InvariantStatement(
            name="numerical_round_trip",
            description="recomposer(decomposer(M)) - M norm is below the "
                        "registered tolerance on the fixture set.",
        ),
    ]


def _map_reduce_invariants() -> list[InvariantStatement]:
    return [
        InvariantStatement(
            name="map_pure",
            description="Mapper is a pure function; no shared state across mappers.",
        ),
        InvariantStatement(
            name="reduce_associative_commutative",
            description="Reducer is associative + commutative.",
        ),
        InvariantStatement(
            name="round_trip",
            description="reduce(map_all) matches ground truth on the registered "
                        "fixture set.",
        ),
    ]


def _composite_invariants() -> list[InvariantStatement]:
    return [
        InvariantStatement(
            name="outer_pattern_invariants_apply",
            description="The outer pattern's invariants apply to the composite.",
        ),
        InvariantStatement(
            name="inner_pattern_invariants_apply",
            description="The inner pattern's invariants apply at each outer-chunk.",
        ),
        InvariantStatement(
            name="recomposer_is_outer_of_inner",
            description="Composite recomposer = outer recomposer applied to "
                        "per-outer-chunk inner-recomposed results.",
        ),
    ]


_CANONICAL_INVARIANTS: dict[str, list[InvariantStatement]] = {
    "embarrassingly_parallel": _embarrassingly_parallel_invariants(),
    "spatial_domain": _spatial_domain_invariants(),
    "temporal_stepping": _temporal_stepping_invariants(),
    "matrix_block": _matrix_block_invariants(),
    "map_reduce": _map_reduce_invariants(),
    "composite": _composite_invariants(),
}


# ---------------------------------------------------------------------------
# Receipts + lookup records
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RegistrationReceipt:
    pattern_name: str
    parameterization_name: str
    revision: int
    registered_at: datetime
    decomposer_qualname: str
    recomposer_qualname: str


@dataclass(frozen=True)
class RegisteredParameterization:
    pattern_name: str
    parameterization_name: str
    decomposer: Decomposer
    recomposer: Recomposer
    additional_invariants: tuple[InvariantStatement, ...] = ()
    revision: int = 1


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


class PatternRegistry:
    """Per spec §3.2.

    - Built-in pattern vocabulary is closed.
    - Parameterizations register against a built-in pattern.
    - Conflicts on (pattern_name, parameterization_name) require
      ``replace=True`` to overwrite; the receipt's revision counter
      bumps each successful overwrite.
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._params: dict[tuple[str, str], RegisteredParameterization] = {}

    @classmethod
    def with_builtins(cls) -> PatternRegistry:
        """Construct a fresh registry. The closed pattern vocabulary
        is implicit (in ``BUILTIN_PATTERN_NAMES``); built-in
        parameterizations are NOT pre-registered — extensions register
        their own."""
        return cls()

    # -- introspection ------------------------------------------------------

    def canonical_invariants(self, pattern_name: str) -> list[InvariantStatement]:
        if pattern_name not in BUILTIN_PATTERN_NAMES:
            raise UnknownPatternError(
                f"unknown pattern {pattern_name!r}; closed vocabulary is "
                f"{sorted(BUILTIN_PATTERN_NAMES)}"
            )
        return list(_CANONICAL_INVARIANTS[pattern_name])

    def list_parameterizations(self, pattern_name: str) -> list[str]:
        if pattern_name not in BUILTIN_PATTERN_NAMES:
            raise UnknownPatternError(
                f"unknown pattern {pattern_name!r}"
            )
        with self._lock:
            return [
                p for (pname, p) in self._params.keys()
                if pname == pattern_name
            ]

    # -- registration -------------------------------------------------------

    def register_parameterization(
        self,
        *,
        pattern_name: str,
        parameterization_name: str,
        decomposer: Decomposer,
        recomposer: Recomposer,
        additional_invariants: Optional[list[InvariantStatement]] = None,
        replace: bool = False,
    ) -> RegistrationReceipt:
        if pattern_name not in BUILTIN_PATTERN_NAMES:
            raise UnknownPatternError(
                f"cannot register against unknown pattern {pattern_name!r}; "
                f"closed vocabulary is {sorted(BUILTIN_PATTERN_NAMES)}"
            )
        key = (pattern_name, parameterization_name)
        with self._lock:
            existing = self._params.get(key)
            if existing is not None and not replace:
                raise PatternConflictError(
                    f"parameterization {parameterization_name!r} already "
                    f"registered for {pattern_name!r}; pass replace=True "
                    f"to overwrite (current rev={existing.revision})"
                )
            new_revision = (existing.revision + 1) if existing else 1
            entry = RegisteredParameterization(
                pattern_name=pattern_name,
                parameterization_name=parameterization_name,
                decomposer=decomposer,
                recomposer=recomposer,
                additional_invariants=tuple(additional_invariants or []),
                revision=new_revision,
            )
            self._params[key] = entry
            return RegistrationReceipt(
                pattern_name=pattern_name,
                parameterization_name=parameterization_name,
                revision=new_revision,
                registered_at=datetime.now(UTC),
                decomposer_qualname=getattr(decomposer, "__qualname__",
                                            type(decomposer).__qualname__),
                recomposer_qualname=getattr(recomposer, "__qualname__",
                                            type(recomposer).__qualname__),
            )

    # -- lookup -------------------------------------------------------------

    def get_parameterization(
        self, pattern_name: str, parameterization_name: str,
    ) -> RegisteredParameterization:
        with self._lock:
            try:
                return self._params[(pattern_name, parameterization_name)]
            except KeyError as e:
                raise KeyError(
                    f"no parameterization {parameterization_name!r} "
                    f"registered for pattern {pattern_name!r}"
                ) from e


# ---------------------------------------------------------------------------
# Process-global default registry (mirrors the public API surface in spec §3)
# ---------------------------------------------------------------------------


_DEFAULT_REGISTRY: Optional[PatternRegistry] = None
_DEFAULT_LOCK = threading.RLock()


def _get_default_registry() -> PatternRegistry:
    global _DEFAULT_REGISTRY
    with _DEFAULT_LOCK:
        if _DEFAULT_REGISTRY is None:
            _DEFAULT_REGISTRY = PatternRegistry.with_builtins()
        return _DEFAULT_REGISTRY


def register_pattern_parameterization(
    *,
    pattern_name: str,
    parameterization_name: str,
    decomposer: Decomposer,
    recomposer: Recomposer,
    additional_invariants: Optional[list[InvariantStatement]] = None,
    replace: bool = False,
    registry: Optional[PatternRegistry] = None,
) -> RegistrationReceipt:
    """Public API surface per spec §3 ``__all__``. Defers to the
    process-default registry unless a specific registry is passed."""
    target = registry or _get_default_registry()
    return target.register_parameterization(
        pattern_name=pattern_name,
        parameterization_name=parameterization_name,
        decomposer=decomposer,
        recomposer=recomposer,
        additional_invariants=additional_invariants,
        replace=replace,
    )
