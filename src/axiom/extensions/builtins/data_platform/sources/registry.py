# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``SourceKindRegistry`` — process-local index of registered providers.

Each :class:`SourceKindProvider` registers itself once (idiomatically at
import time from its package's ``__init__.py``). The CLI's ``axi data
register`` subparser walks this registry to render sub-subcommands;
the Dagster sensor walks it to construct sources from saved connector
configs.

A thin specialization of the shared :class:`KindRegistry` (ADR-110
§Decision-1): namespaced by kind, raises on duplicate registration (loud),
single process-local default singleton with a ``default_source_kind_registry()``
accessor.
"""

from __future__ import annotations

from ..kind_registry import KindRegistry
from .contracts import SourceKindProvider


class SourceKindRegistry(KindRegistry):
    def __init__(self) -> None:
        super().__init__(SourceKindProvider, "source")


_default: SourceKindRegistry | None = None


def default_source_kind_registry() -> SourceKindRegistry:
    """Return the process-local default registry.

    Provider packages register here at import. Tests should build
    their own via ``SourceKindRegistry()`` to stay isolated.
    """
    global _default
    if _default is None:
        _default = SourceKindRegistry()
    return _default


__all__ = ["SourceKindRegistry", "default_source_kind_registry"]
