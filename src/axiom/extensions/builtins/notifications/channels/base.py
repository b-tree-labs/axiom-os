# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Channel-adapter base protocol + registry — HERALD's factory/provider layer.

Mirrors the secrets-extension SecretBackendProvider shape (PR #296). A
``ChannelAdapterProvider`` is the factory that advertises ``Capabilities``
and builds a runtime ``ChannelAdapter`` from a config dict. Adding a new
channel = a new package that registers a provider at import time. No
platform-code change.

See ``../docs/decisions/adr-001-channel-adapter-as-aeos-kind.md`` for the
locked decision: ``kind = "channel_adapter"`` is the 8th AEOS capability
kind, not a subtype of ``adapter``.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from enum import Enum
from typing import Any, Protocol, runtime_checkable

from axiom.governance import Classification, classification_lte


class Direction(str, Enum):
    OUTBOUND = "outbound"
    INBOUND = "inbound"
    BIDIRECTIONAL = "bidirectional"


@dataclass(frozen=True)
class ChannelCapabilities:
    """The static capability declaration for a channel adapter family.

    Mirrors the ``[extension.provides.channel_adapter]`` block in the
    AEOS manifest. ``axi ext lint --strict`` checks the manifest against
    this dataclass when an adapter is installed.
    """

    name: str
    direction: Direction
    priority_levels: tuple[str, ...]
    classification_ceiling: Classification
    supports_threading: bool
    supports_acknowledge: bool
    delivery_sla_p95_ms: int
    connector_ref: str | None = None
    webhook_path: str | None = None


@runtime_checkable
class ChannelAdapter(Protocol):
    """Runtime adapter — stateless across sends; receives capability per call.

    Real implementations (SlackChannelAdapter, etc.) land in HERALD-2.
    SEC-1 ships only the inbox adapter; see ``inbox.py``.
    """

    name: str


@runtime_checkable
class ChannelAdapterProvider(Protocol):
    """Factory shape every channel adapter package implements."""

    @property
    def name(self) -> str: ...

    def capabilities(self) -> ChannelCapabilities: ...

    def build(self, config: dict[str, Any]) -> ChannelAdapter: ...


class ChannelAdapterRegistry:
    """Provider-driven registry. The CLI + send() consult it at dispatch."""

    def __init__(self) -> None:
        self._providers: dict[str, ChannelAdapterProvider] = {}

    def register(
        self,
        provider: ChannelAdapterProvider,
        *,
        replace: bool = False,
    ) -> None:
        if provider.name in self._providers and not replace:
            raise ValueError(
                f"channel adapter {provider.name!r} already registered; "
                "pass replace=True to override"
            )
        self._providers[provider.name] = provider

    def get(self, name: str) -> ChannelAdapterProvider:
        if name not in self._providers:
            raise KeyError(f"no channel adapter registered for {name!r}")
        return self._providers[name]

    def names(self) -> list[str]:
        return sorted(self._providers)

    def all(self) -> Iterable[ChannelAdapterProvider]:
        return list(self._providers.values())

    def admitted_for(
        self, classification: Classification
    ) -> list[ChannelAdapterProvider]:
        """Return providers whose ceiling admits ``classification``.

        Centralized classification routing (spec §4): the registry is the
        sole site that does the ceiling comparison. Adapter code never
        sees an envelope above its ceiling.
        """
        return [
            p
            for p in self._providers.values()
            if classification_lte(
                classification, p.capabilities().classification_ceiling
            )
        ]


__all__ = [
    "ChannelAdapter",
    "ChannelAdapterProvider",
    "ChannelAdapterRegistry",
    "ChannelCapabilities",
    "Direction",
]
