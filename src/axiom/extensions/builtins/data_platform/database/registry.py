# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``DatabaseKindRegistry`` — process-local index of registered DB
providers. Mirrors :class:`SourceKindRegistry`."""

from __future__ import annotations

from .contracts import DatabaseKindProvider


class DatabaseKindRegistry:
    def __init__(self) -> None:
        self._providers: dict[str, DatabaseKindProvider] = {}

    def register(self, provider: DatabaseKindProvider) -> None:
        if not isinstance(provider, DatabaseKindProvider):
            raise TypeError(
                f"object does not satisfy DatabaseKindProvider protocol: {provider!r}"
            )
        if not provider.kind:
            raise ValueError("provider must declare a non-empty kind")
        if provider.kind in self._providers:
            raise ValueError(
                f"database kind {provider.kind!r} is already registered"
            )
        self._providers[provider.kind] = provider

    def get(self, kind: str) -> DatabaseKindProvider:
        try:
            return self._providers[kind]
        except KeyError:
            raise KeyError(
                f"no database-kind provider for {kind!r}; "
                f"known kinds: {sorted(self._providers)}"
            ) from None

    def kinds(self) -> list[str]:
        return sorted(self._providers)

    def has(self, kind: str) -> bool:
        return kind in self._providers


_default: DatabaseKindRegistry | None = None


def default_database_kind_registry() -> DatabaseKindRegistry:
    global _default
    if _default is None:
        _default = DatabaseKindRegistry()
    return _default


__all__ = ["DatabaseKindRegistry", "default_database_kind_registry"]
