# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``DatabaseKindRegistry`` — process-local index of registered DB providers.
A thin specialization of the shared :class:`KindRegistry` (ADR-110 §Decision-1)."""

from __future__ import annotations

from ..kind_registry import KindRegistry
from .contracts import DatabaseKindProvider


class DatabaseKindRegistry(KindRegistry):
    def __init__(self) -> None:
        super().__init__(DatabaseKindProvider, "database")


_default: DatabaseKindRegistry | None = None


def default_database_kind_registry() -> DatabaseKindRegistry:
    global _default
    if _default is None:
        _default = DatabaseKindRegistry()
    return _default


__all__ = ["DatabaseKindRegistry", "default_database_kind_registry"]
