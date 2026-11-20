# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""INS-003: a kind name means one kind (spec-instrument §Declaring a kind).

Two packages declaring one name: neither is used and both are named, because
picking one would let install order decide what a person's command runs. In
process, registering a different object under a live name raises (ADR-110 §1), and
the same object again is harmless.
"""

from __future__ import annotations

import pytest

from axiom.extensions.builtins.instrument import registry

from .fakes import GAUGES, HERE, GaugeKind, entry_point, extension


def test_a_name_declared_by_two_origins_is_used_by_neither(builtins):
    ext = extension(builtins / "gauges-ext", [{"name": "gauges", "entry": f"{HERE}:GAUGES"}])
    ep = entry_point("gauges", f"{HERE}:OTHER_GAUGES", "acme-gauges")
    cat = registry.discover_kinds(extensions=[ext], entry_points=[ep], allow=("acme-gauges",))
    assert cat.names() == []
    (row,) = [r for r in cat.rows() if r["name"] == "gauges"]
    assert row["state"] == "conflict"
    assert "builtin" in row["origin"] and "acme-gauges" in row["origin"]
    with pytest.raises(registry.InstrumentKindError) as exc:
        cat.kind("gauges")
    assert "builtin" in str(exc.value) and "acme-gauges" in str(exc.value)
    assert exc.value.fix


@pytest.mark.parametrize("name", ["list", "status", "help"])
def test_a_reserved_name_is_refused(builtins, name):
    ext = extension(builtins / "gauges-ext", [{"name": name, "entry": f"{HERE}:GAUGES"}])
    cat = registry.discover_kinds(extensions=[ext], entry_points=[], allow=())
    assert cat.names() == []
    (row,) = cat.rows()
    assert row["state"] == "refused" and "reserved" in row["reason"]


@pytest.mark.parametrize("name", ["Gauges", "-gauges", "gauges!", "a" * 25, ""])
def test_a_malformed_name_is_refused(builtins, name):
    ext = extension(builtins / "gauges-ext", [{"name": name, "entry": f"{HERE}:GAUGES"}])
    cat = registry.discover_kinds(extensions=[ext], entry_points=[], allow=())
    assert cat.names() == []
    assert cat.rows()[0]["state"] == "refused"


def test_a_block_without_a_valid_entry_is_refused(builtins):
    ext = extension(builtins / "gauges-ext", [{"name": "gauges", "entry": "not an entry"}])
    cat = registry.discover_kinds(extensions=[ext], entry_points=[], allow=())
    assert cat.rows()[0]["state"] == "refused"
    assert "pkg.module:attr" in cat.rows()[0]["reason"]


def test_an_entry_whose_kind_has_another_name_is_not_used(builtins):
    ext = extension(builtins / "gauges-ext", [{"name": "gauges", "entry": f"{HERE}:WRONG_NAME"}])
    cat = registry.discover_kinds(extensions=[ext], entry_points=[], allow=())
    with pytest.raises(registry.InstrumentKindError) as exc:
        cat.kind("gauges")
    assert "meters" in str(exc.value)
    assert [r["state"] for r in cat.rows()] == ["broken"]


def test_registering_another_object_under_a_live_name_raises():
    cat = registry.KindCatalog()
    cat.register(GAUGES)
    cat.register(GAUGES)  # the same object again: a no-op
    with pytest.raises(ValueError):
        cat.register(GaugeKind())
    assert cat.names() == ["gauges"]


def test_registering_over_a_discovered_name_raises(builtins):
    ext = extension(builtins / "gauges-ext", [{"name": "gauges", "entry": f"{HERE}:GAUGES"}])
    cat = registry.discover_kinds(extensions=[ext], entry_points=[], allow=())
    with pytest.raises(ValueError):
        cat.register(GaugeKind())
    cat.register(GAUGES)  # the very kind discovery found: harmless
    assert cat.kind("gauges") is GAUGES


def test_registering_a_reserved_or_malformed_name_raises():
    cat = registry.KindCatalog()
    for name in ("list", "Bad Name"):
        with pytest.raises(ValueError):
            cat.register(GaugeKind(name=name))


def test_registering_something_that_is_not_a_kind_raises():
    from .fakes import NOT_A_KIND

    with pytest.raises(TypeError):
        registry.KindCatalog().register(NOT_A_KIND)
