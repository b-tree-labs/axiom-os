# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``SourceKindRegistry`` — process-local index of registered providers.

Each :class:`SourceKindProvider` registers itself once (idiomatically at
import time from its package's ``__init__.py``). The CLI's ``axi data
register`` subparser walks this registry to render sub-subcommands;
the Dagster sensor walks it to construct sources from saved connector
configs.

Same shape as :class:`axiom.infra.skills.SkillRegistry`: namespaced by
kind, raises on duplicate registration (loud), single process-local
default singleton with a ``default_source_kind_registry()`` accessor.
"""

from __future__ import annotations

from .contracts import SourceKindProvider


class SourceKindRegistry:
    """Index of registered source-kind providers."""

    def __init__(self) -> None:
        self._providers: dict[str, SourceKindProvider] = {}

    def register(self, provider: SourceKindProvider) -> None:
        """Register a provider under its declared ``kind``."""
        if not isinstance(provider, SourceKindProvider):
            raise TypeError(
                f"object does not satisfy SourceKindProvider protocol: {provider!r}"
            )
        if not provider.kind:
            raise ValueError("provider must declare a non-empty kind")
        if provider.kind in self._providers:
            raise ValueError(
                f"source kind {provider.kind!r} is already registered "
                f"(double-import? two providers competing for the kind?)"
            )
        self._providers[provider.kind] = provider

    def get(self, kind: str) -> SourceKindProvider:
        """Return the provider for ``kind`` (raises KeyError)."""
        try:
            return self._providers[kind]
        except KeyError:
            raise KeyError(
                f"no source-kind provider registered for {kind!r}; "
                f"known kinds: {sorted(self._providers)}"
            ) from None

    def kinds(self) -> list[str]:
        """Return registered kind names, sorted."""
        return sorted(self._providers)

    def has(self, kind: str) -> bool:
        return kind in self._providers


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
