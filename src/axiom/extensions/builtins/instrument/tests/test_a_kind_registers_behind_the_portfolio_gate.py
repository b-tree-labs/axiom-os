# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""INS-002: a kind comes in by manifest block or entry point, through the portfolio gate,
before anything is imported (ADR-256 D3, ADR-023-A1 A1.3).

Where a declaration lives decides whether it is trusted, never what it says about
itself, and Axiom's own kinds use the same door a consumer's kind does.
"""

from __future__ import annotations

import sys

import pytest

from axiom.extensions.builtins.instrument import registry

from .fakes import GAUGES, HERE, entry_point, extension


def _state(cat: registry.KindCatalog, name: str) -> dict:
    (row,) = [r for r in cat.rows() if r["name"] == name]
    return row


def test_a_kind_in_a_builtin_manifest_registers(builtins):
    ext = extension(builtins / "gauges-ext", [{"name": "gauges", "entry": f"{HERE}:GAUGES"}])
    cat = registry.discover_kinds(extensions=[ext], entry_points=[], allow=())
    assert cat.names() == ["gauges"]
    assert cat.kind("gauges") is GAUGES
    row = _state(cat, "gauges")
    assert row["state"] == "registered" and row["origin"] == "builtin"
    assert row["yields"] == "readings"


def test_a_kind_from_a_portfolio_distributions_entry_point_registers(builtins):
    ep = entry_point("gauges", f"{HERE}:GAUGES", "acme-gauges")
    cat = registry.discover_kinds(extensions=[], entry_points=[ep], allow=("acme-gauges",))
    assert cat.kind("gauges") is GAUGES
    assert "acme-gauges" in _state(cat, "gauges")["origin"]


def test_a_kind_from_a_portfolio_distributions_manifest_registers(tmp_path, builtins):
    root = tmp_path / "site" / "consumer_pkg" / "extensions" / "builtins" / "gauges"
    ext = extension(root, [{"name": "gauges", "entry": f"{HERE}:GAUGES"}])
    cat = registry.discover_kinds(
        extensions=[ext],
        entry_points=[],
        allow=("acme-gauges",),
        packages={"consumer_pkg": ["acme-gauges"]},
    )
    assert cat.kind("gauges") is GAUGES


def test_an_entry_point_from_any_other_distribution_is_refused_before_import(builtins):
    module = "axiom_instrument_canary_never_imported"
    ep = entry_point("gauges", f"{module}:KIND", "someone-elses")
    cat = registry.discover_kinds(extensions=[], entry_points=[ep], allow=("acme-gauges",))
    assert cat.names() == []
    row = _state(cat, "gauges")
    # refused, not broken: nothing tried to import it
    assert row["state"] == "refused" and "portfolio" in row["reason"]
    assert module not in sys.modules
    with pytest.raises(registry.InstrumentKindError) as exc:
        cat.kind("gauges")
    assert "portfolio" in str(exc.value)


def test_a_manifest_outside_builtins_and_any_portfolio_package_is_refused(tmp_path, builtins):
    # A project or user extension directory is inside no package, so no distribution
    # vouches for it, whatever its manifest says about itself.
    ext = extension(
        tmp_path / "project" / ".axi" / "extensions" / "gauges",
        [{"name": "gauges", "entry": "axiom_instrument_canary_never_imported:KIND"}],
    )
    cat = registry.discover_kinds(extensions=[ext], entry_points=[], allow=("acme-gauges",))
    assert cat.names() == []
    assert _state(cat, "gauges")["state"] == "refused"


def test_a_disabled_extension_declares_no_kind(builtins):
    ext = extension(builtins / "gauges-ext", [{"name": "gauges", "entry": f"{HERE}:GAUGES"}])
    ext.enabled = False
    cat = registry.discover_kinds(extensions=[ext], entry_points=[], allow=())
    assert cat.rows() == []


def test_the_entry_point_group_and_block_are_the_documented_ones():
    assert registry.ENTRY_POINT_GROUP == "axiom.instrument_kinds"
    assert registry.MANIFEST_BLOCK == "instrument_kinds"


def test_discovery_is_kept_for_the_process_until_refreshed(monkeypatch):
    seen = []

    def fake_discover(**_kw):
        seen.append(1)
        return registry.KindCatalog()

    monkeypatch.setattr(registry, "discover_kinds", fake_discover)
    monkeypatch.setattr(registry, "_CACHE", None)
    first = registry.catalog()
    assert registry.catalog() is first and len(seen) == 1
    assert registry.catalog(refresh=True) is not first and len(seen) == 2
