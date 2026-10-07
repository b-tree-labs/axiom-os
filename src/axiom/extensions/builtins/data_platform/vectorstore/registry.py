# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``VectorStoreRegistry`` — a thin specialization of the shared
:class:`KindRegistry` (ADR-110 §Decision-1)."""

from __future__ import annotations

from ..kind_registry import KindRegistry
from .contracts import VectorStoreProvider


class VectorStoreRegistry(KindRegistry):
    def __init__(self) -> None:
        super().__init__(VectorStoreProvider, "vector-store")


_default: VectorStoreRegistry | None = None


def default_vector_store_registry() -> VectorStoreRegistry:
    global _default
    if _default is None:
        _default = VectorStoreRegistry()
    return _default


__all__ = ["VectorStoreRegistry", "default_vector_store_registry"]
