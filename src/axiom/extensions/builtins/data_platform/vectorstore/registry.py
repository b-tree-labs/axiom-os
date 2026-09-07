# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``VectorStoreRegistry`` — process-local index. Mirrors
:class:`DatabaseKindRegistry`."""

from __future__ import annotations

from .contracts import VectorStoreProvider


class VectorStoreRegistry:
    def __init__(self) -> None:
        self._providers: dict[str, VectorStoreProvider] = {}

    def register(self, provider: VectorStoreProvider) -> None:
        if not isinstance(provider, VectorStoreProvider):
            raise TypeError(
                f"object does not satisfy VectorStoreProvider protocol: {provider!r}"
            )
        if not provider.kind:
            raise ValueError("provider must declare a non-empty kind")
        if provider.kind in self._providers:
            raise ValueError(
                f"vector-store kind {provider.kind!r} is already registered"
            )
        self._providers[provider.kind] = provider

    def get(self, kind: str) -> VectorStoreProvider:
        try:
            return self._providers[kind]
        except KeyError:
            raise KeyError(
                f"no vector-store provider for {kind!r}; "
                f"known kinds: {sorted(self._providers)}"
            ) from None

    def kinds(self) -> list[str]:
        return sorted(self._providers)

    def has(self, kind: str) -> bool:
        return kind in self._providers


_default: VectorStoreRegistry | None = None


def default_vector_store_registry() -> VectorStoreRegistry:
    global _default
    if _default is None:
        _default = VectorStoreRegistry()
    return _default


__all__ = ["VectorStoreRegistry", "default_vector_store_registry"]
