# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tests for ChannelAdapterRegistry — the factory/provider pattern that
mirrors the secrets-extension SecretBackendProvider shape (PR #296).

TDD-first per CLAUDE.md core invariants: registry contract pinned here
before any real adapter ships.
"""

from __future__ import annotations

import pytest

from axiom.extensions.builtins.notifications.channels.base import (
    ChannelAdapter,
    ChannelAdapterRegistry,
    ChannelCapabilities,
    Direction,
)
from axiom.governance import Classification


class _FakeAdapter:
    """Minimal runtime adapter for registry testing."""

    def __init__(self, name: str) -> None:
        self.name = name


class _FakeProvider:
    """Minimal provider — satisfies ChannelAdapterProvider protocol."""

    def __init__(self, name: str, ceiling: Classification) -> None:
        self._name = name
        self._ceiling = ceiling

    @property
    def name(self) -> str:
        return self._name

    def capabilities(self) -> ChannelCapabilities:
        return ChannelCapabilities(
            name=self._name,
            direction=Direction.OUTBOUND,
            priority_levels=("normal",),
            classification_ceiling=self._ceiling,
            supports_threading=False,
            supports_acknowledge=False,
            delivery_sla_p95_ms=2000,
        )

    def build(self, config: dict) -> ChannelAdapter:
        return _FakeAdapter(self._name)  # type: ignore[return-value]


class TestRegistry:
    def test_empty_registry_returns_empty_list(self) -> None:
        reg = ChannelAdapterRegistry()
        assert reg.names() == []

    def test_register_then_get_provider(self) -> None:
        reg = ChannelAdapterRegistry()
        provider = _FakeProvider("slack", Classification.INTERNAL)
        reg.register(provider)
        assert "slack" in reg.names()
        assert reg.get("slack") is provider

    def test_get_missing_raises(self) -> None:
        reg = ChannelAdapterRegistry()
        with pytest.raises(KeyError):
            reg.get("nope")

    def test_double_register_raises_unless_replace(self) -> None:
        reg = ChannelAdapterRegistry()
        p1 = _FakeProvider("slack", Classification.INTERNAL)
        p2 = _FakeProvider("slack", Classification.PUBLIC)
        reg.register(p1)
        with pytest.raises(ValueError):
            reg.register(p2)
        reg.register(p2, replace=True)
        assert reg.get("slack") is p2

    def test_admitted_for_classification(self) -> None:
        reg = ChannelAdapterRegistry()
        reg.register(_FakeProvider("inbox", Classification.CONTROLLED))
        reg.register(_FakeProvider("slack", Classification.INTERNAL))
        admitted = reg.admitted_for(Classification.REGULATED)
        names = {p.name for p in admitted}
        assert names == {"inbox"}

    def test_capabilities_round_trip(self) -> None:
        cap = ChannelCapabilities(
            name="slack",
            direction=Direction.BIDIRECTIONAL,
            priority_levels=("low", "normal", "high"),
            classification_ceiling=Classification.INTERNAL,
            supports_threading=True,
            supports_acknowledge=True,
            delivery_sla_p95_ms=1500,
        )
        assert cap.classification_ceiling is Classification.INTERNAL
        assert cap.direction is Direction.BIDIRECTIONAL
