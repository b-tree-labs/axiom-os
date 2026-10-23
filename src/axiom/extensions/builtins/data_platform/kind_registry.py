# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``KindRegistry`` — the shared ``kind`` → provider index for the data platform
(ADR-110 §Decision-1). Database, source, and vector-store registries were three
byte-near-identical classes; this is that shape once, on the one
``ConnectorRegistry`` mechanism, parameterized by the provider Protocol and a
label. Providers are instances keyed by their declared ``.kind``."""

from __future__ import annotations

from typing import Any

from axiom.infra.connector_registry import ConnectorRegistry


class KindRegistry:
    """Instance registry keyed by ``provider.kind``, validated against a Protocol."""

    def __init__(self, protocol: type, label: str) -> None:
        self._protocol = protocol
        self._label = label
        self._reg: ConnectorRegistry = ConnectorRegistry(f"{label} kind")

    def register(self, provider: Any) -> None:
        if not isinstance(provider, self._protocol):
            raise TypeError(
                f"object does not satisfy {self._protocol.__name__} protocol: {provider!r}"
            )
        if not provider.kind:
            raise ValueError("provider must declare a non-empty kind")
        if provider.kind in self._reg.available():
            raise ValueError(f"{self._label} kind {provider.kind!r} is already registered")
        self._reg.register(provider.kind, provider)

    def get(self, kind: str) -> Any:
        try:
            return self._reg.get(kind)
        except KeyError:
            raise KeyError(
                f"no {self._label} provider for {kind!r}; "
                f"known kinds: {sorted(self._reg.available())}"
            ) from None

    def kinds(self) -> list[str]:
        return list(self._reg.available())

    def has(self, kind: str) -> bool:
        return kind in self._reg.available()


__all__ = ["KindRegistry"]
