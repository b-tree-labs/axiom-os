# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Pluggable priority strategies for hook execution order.

See ``docs/specs/spec-hooks.md`` §5.5. v1 ships two strategies:
``ManifestPriorityStrategy`` (default — order by declared priority) and
``TrustWeightedStrategy`` (higher-trust extensions run first).
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import Protocol, runtime_checkable

from axiom.infra.hooks.types import HookSpec


@runtime_checkable
class PriorityStrategy(Protocol):
    """Strategy seam for ordering hooks within a single event.

    Implementations must be deterministic for a given input set so that
    concurrent fires of the same event observe a consistent order.
    """

    name: str

    def order(self, hooks: Iterable[HookSpec]) -> list[HookSpec]:
        """Return hooks in execution order. Index 0 runs first."""
        ...


class ManifestPriorityStrategy:
    """Order hooks by manifest-declared priority. Ties broken by source.

    AEOS §4.7 default. Lower ``priority`` value runs first (matching the
    Linux nice / systemd convention).
    """

    name = "manifest_priority"

    def order(self, hooks: Iterable[HookSpec]) -> list[HookSpec]:
        return sorted(hooks, key=lambda h: (h.priority, h.source))


class TrustWeightedStrategy:
    """Higher-trust extensions run before lower-trust ones; ties by priority.

    Trust scores come from a caller-supplied lookup so this module never
    imports from the trust graph directly. Useful when a deployment wants
    signed-by-the-institution hooks to always pre-empt user-installed ones.
    """

    name = "trust_weighted"

    def __init__(self, trust_lookup: Callable[[str], int]) -> None:
        self._trust = trust_lookup

    def order(self, hooks: Iterable[HookSpec]) -> list[HookSpec]:
        # Negate trust so higher trust sorts earlier; priority breaks ties.
        return sorted(hooks, key=lambda h: (-self._trust(h.source), h.priority, h.source))


__all__ = [
    "ManifestPriorityStrategy",
    "PriorityStrategy",
    "TrustWeightedStrategy",
]
