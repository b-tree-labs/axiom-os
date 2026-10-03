# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""The one registry mechanism every connector family shares (ADR-110 §Decision-1).

Five families (secrets, directory, calendar, storage, channels) each re-derived
"a kind->factory map, loud on duplicate, with a clear error listing known kinds".
This is that mechanism, once. The unknown-kind error is BOTH a KeyError and a
ValueError so a family converging onto it keeps whichever contract its callers
already depend on."""

from __future__ import annotations

import pytest

from axiom.infra.connector_registry import ConnectorRegistry, UnknownConnectorKind


def test_register_get_and_create():
    reg: ConnectorRegistry = ConnectorRegistry("widget")
    reg.register("a", lambda **c: ("A", c))
    assert reg.available() == ("a",)
    assert reg.get("a") is not None
    assert reg.create("a", x=1) == ("A", {"x": 1})


def test_available_is_sorted():
    reg: ConnectorRegistry = ConnectorRegistry("widget")
    for k in ("m365", "google", "caldav"):
        reg.register(k, object)
    assert list(reg.available()) == sorted(("m365", "google", "caldav"))


def test_duplicate_is_loud_but_idempotent_for_same_factory():
    reg: ConnectorRegistry = ConnectorRegistry("widget")
    f = object
    reg.register("a", f)
    reg.register("a", f)  # same factory again — idempotent, no raise
    with pytest.raises(ValueError, match="already registered"):
        reg.register("a", dict)  # different factory — refuse to clobber


def test_replace_true_allows_override():
    reg: ConnectorRegistry = ConnectorRegistry("widget")
    reg.register("a", object)
    reg.register("a", dict, replace=True)
    assert reg.get("a") is dict


def test_unknown_kind_error_is_both_keyerror_and_valueerror():
    reg: ConnectorRegistry = ConnectorRegistry("directory provider")
    with pytest.raises(UnknownConnectorKind) as ei:
        reg.get("nope")
    assert isinstance(ei.value, KeyError) and isinstance(ei.value, ValueError)
    assert "directory provider" in str(ei.value) and "nope" in str(ei.value)
    # a family that catches ValueError still catches it:
    with pytest.raises(ValueError):
        reg.create("nope")


def test_empty_kind_rejected():
    reg: ConnectorRegistry = ConnectorRegistry("widget")
    with pytest.raises(ValueError, match="non-empty"):
        reg.register("", object)


def test_unregister():
    reg: ConnectorRegistry = ConnectorRegistry("widget")
    reg.register("a", object)
    reg.unregister("a")
    assert reg.available() == ()


def test_values_and_items_expose_registered_entries():
    """Instance-registries (e.g. channel adapters store live providers, not
    factories) need to iterate the registered values — the one mechanism serves
    both factory- and instance-shaped families (ADR-110 §Decision-1)."""
    reg: ConnectorRegistry = ConnectorRegistry("widget")
    reg.register("a", "AA")
    reg.register("b", "BB")
    assert sorted(reg.values()) == ["AA", "BB"]
    assert dict(reg.items()) == {"a": "AA", "b": "BB"}
