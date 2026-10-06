# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0
"""Normalizer discovery is a security boundary (ADR-023-A1 §A1.3).

    Fixtures use neutral names on purpose: this mechanism must not know that
    any particular consumer product exists, and the public-mirror guard
    enforces that it does not.

One conform process reads every tenant's bronze rows in one interpreter, so a
normalizer from a site package would be one institution's code running against
another's data. These tests pin the refusal, not just the happy path.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from axiom.extensions.builtins.data_platform.conformance import discovery


class _Registry:
    def __init__(self) -> None:
        self.registered: list[str] = []

    def register(self, schema_ref, normalizer):  # noqa: ANN001
        self.registered.append(schema_ref)


def _ep(name: str, dist_name: str, fn):
    return SimpleNamespace(name=name, dist=SimpleNamespace(name=dist_name), load=lambda: fn)


@pytest.fixture
def eps(monkeypatch):
    """Install a fake entry-point table for the normalizer group."""

    def install(entries):
        monkeypatch.setattr(
            discovery,
            "entry_points",
            lambda group: entries if group == discovery.NORMALIZER_GROUP else [],
        )

    return install


def test_platform_normalizer_is_registered(eps) -> None:
    def register(registry):
        registry.register("example-facility/signal-v1", object())

    eps([_ep("domain_pkg", "example-domain", register)])
    registry = _Registry()
    loaded = discovery.register_discovered(registry, allow=frozenset({"example-domain"}))
    assert loaded == ["example-domain"]
    assert registry.registered == ["example-facility/signal-v1"]


def test_site_package_normalizer_is_refused_and_never_loaded(eps) -> None:
    """The whole point: refusing to import, because importing is execution."""
    loaded_flag = {"imported": False}

    def should_never_run(registry):  # pragma: no cover - must not be called
        loaded_flag["imported"] = True
        registry.register("evil/v1", object())

    ep = _ep("site_pkg", "example-site", should_never_run)
    eps([ep])
    registry = _Registry()
    loaded = discovery.register_discovered(registry, allow=frozenset({"example-domain"}))
    assert loaded == []
    assert registry.registered == []
    assert loaded_flag["imported"] is False


def test_an_entry_point_with_no_distribution_is_refused(eps) -> None:
    ep = SimpleNamespace(name="orphan", dist=None, load=lambda: lambda r: None)
    eps([ep])
    assert discovery.register_discovered(_Registry(), allow=frozenset({"example-domain"})) == []


def test_one_broken_platform_package_does_not_stop_the_others(eps) -> None:
    def boom(registry):
        raise RuntimeError("bad import")

    def fine(registry):
        registry.register("example-facility/state-v1", object())

    eps([_ep("broken", "example-domain", boom), _ep("good", "example-platform", fine)])
    registry = _Registry()
    loaded = discovery.register_discovered(
        registry, allow=frozenset({"example-domain", "example-platform"})
    )
    assert loaded == ["example-platform"]
    assert registry.registered == ["example-facility/state-v1"]


def test_distribution_names_normalize(eps) -> None:
    """`Example_Domain` and `example-domain` are one distribution (PEP 503)."""

    def register(registry):
        registry.register("x/y-v1", object())

    eps([_ep("n", "Example_Domain", register)])
    registry = _Registry()
    assert discovery.register_discovered(registry, allow=frozenset({"example-domain"})) == [
        "example-domain"
    ]


def test_lookup_failure_returns_empty_rather_than_raising(monkeypatch) -> None:
    def boom(group):
        raise RuntimeError("metadata unavailable")

    monkeypatch.setattr(discovery, "entry_points", boom)
    assert discovery.register_discovered(_Registry(), allow=frozenset({"x"})) == []
    assert discovery.portfolio_distributions() == frozenset()


def test_portfolio_membership_is_what_marks_platform_code(monkeypatch) -> None:
    monkeypatch.setattr(
        discovery,
        "entry_points",
        lambda group: (
            [_ep("m", "example-domain", None), _ep("m2", "example-platform", None)]
            if group == discovery.PORTFOLIO_GROUP
            else []
        ),
    )
    assert discovery.portfolio_distributions() == frozenset({"example-domain", "example-platform"})
