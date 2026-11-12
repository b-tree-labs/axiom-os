# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""A storage provider is a package: discovered, gated, never named by core (ADR-220 §1, §4)."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from axiom.extensions.builtins.connector.storage import registry


class _EP:
    def __init__(self, name: str, dist: str, hook) -> None:
        self.name, self.dist, self._hook = name, SimpleNamespace(name=dist), hook

    def load(self):
        return self._hook


@pytest.fixture
def entry_points(monkeypatch):
    """Replace the installed entry points with the ones a test lists."""
    listed: list[_EP] = []
    monkeypatch.setattr(
        registry,
        "entry_points",
        lambda group: [e for e in listed if group == registry.PROVIDER_GROUP],
    )
    yield listed
    listed.clear()
    for kind in ("ext-store", "outsider-store"):
        registry._STORES.unregister(kind)  # noqa: SLF001 - leave the process registry as found
    registry.rediscover()


def _hook(kind: str, calls: list | None = None):
    def register(r) -> None:
        if calls is not None:
            calls.append(kind)
        r.register(kind, lambda **settings: ("built", kind, settings))

    return register


def test_the_platforms_own_providers_register_from_their_packages(entry_points):
    loaded = registry.discover(force=True, allow=frozenset())
    assert {"box", "local"} <= set(loaded)


def test_an_extension_ships_a_provider_through_the_entry_point_group(entry_points):
    entry_points.append(_EP("ext-store", "axiom-ext-store", _hook("ext-store")))
    registry.discover(force=True, allow=frozenset({"axiom-ext-store"}))
    assert "ext-store" in registry.available_storage_connectors()
    assert registry.get_storage_connector("ext-store", bucket="b") == (
        "built",
        "ext-store",
        {"bucket": "b"},
    )


def test_a_provider_from_outside_the_portfolio_is_never_imported(entry_points, caplog):
    calls: list = []
    entry_points.append(_EP("outsider-store", "someone-elses-pkg", _hook("outsider-store", calls)))
    registry.discover(force=True, allow=frozenset({"axiom-ext-store"}))
    assert calls == [] and "outsider-store" not in registry.available_storage_connectors()
    assert "not a portfolio distribution" in caplog.text


def test_one_broken_provider_does_not_stop_the_rest(entry_points):
    def broken(_r):
        raise RuntimeError("bad package")

    entry_points.append(_EP("broken", "axiom-ext-store", broken))
    entry_points.append(_EP("ext-store", "axiom-ext-store", _hook("ext-store")))
    loaded = registry.discover(force=True, allow=frozenset({"axiom-ext-store"}))
    assert "ext-store" in loaded and "broken" not in loaded


def test_a_provider_installed_later_is_picked_up_by_a_reload(entry_points, monkeypatch):
    monkeypatch.setattr(registry, "_portfolio", lambda: frozenset({"axiom-ext-store"}))
    registry.discover(force=True)
    assert "ext-store" not in registry.available_storage_connectors()
    entry_points.append(_EP("ext-store", "axiom-ext-store", _hook("ext-store")))
    registry.rediscover()
    assert "ext-store" in registry.available_storage_connectors()
