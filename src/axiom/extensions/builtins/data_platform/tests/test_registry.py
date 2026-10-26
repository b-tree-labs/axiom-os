# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tests for the data-platform contribution registry.

The registry is the *home* a consumer layer registers into: ingest
sources, schema packs, and transform packs. These tests pin the
register / lookup / list contract with in-test fakes — no heavy
lakehouse dependency is imported.
"""

from __future__ import annotations

from datetime import datetime

import pytest

# --- Fakes a consumer layer would supply -------------------------------


class FakeIngestSource:
    """A pollable source standing in for a real consumer-layer source."""

    name = "fake-source"

    def __init__(self) -> None:
        self.fetched: list[str] = []

    def list_changed(self, since: datetime | None = None) -> list[str]:
        return ["item-1", "item-2"]

    def fetch(self, item: str) -> bytes:
        self.fetched.append(item)
        return f"payload::{item}".encode()


class FakeSchemaPack:
    name = "bronze-events"
    layer = "bronze"

    def fields(self) -> dict[str, str]:
        return {"id": "string", "ts": "timestamp"}


class FakeTransformPack:
    name = "bronze-to-silver"
    source_layer = "bronze"
    target_layer = "silver"


# --- Tests -------------------------------------------------------------


def _registry():
    from axiom.extensions.builtins.data_platform import DataPlatformRegistry

    return DataPlatformRegistry()


def test_register_and_get_ingest_source():
    reg = _registry()
    src = FakeIngestSource()
    reg.register_source(src)
    assert reg.get_source("fake-source") is src


def test_list_sources_returns_registered():
    reg = _registry()
    src = FakeIngestSource()
    reg.register_source(src)
    names = [s.name for s in reg.list_sources()]
    assert names == ["fake-source"]


def test_register_and_get_schema_pack():
    reg = _registry()
    pack = FakeSchemaPack()
    reg.register_schema_pack(pack)
    assert reg.get_schema_pack("bronze-events") is pack
    assert [p.name for p in reg.list_schema_packs()] == ["bronze-events"]


def test_register_and_get_transform_pack():
    reg = _registry()
    pack = FakeTransformPack()
    reg.register_transform_pack(pack)
    assert reg.get_transform_pack("bronze-to-silver") is pack
    assert [p.name for p in reg.list_transform_packs()] == ["bronze-to-silver"]


def test_get_unknown_source_raises_keyerror():
    reg = _registry()
    with pytest.raises(KeyError):
        reg.get_source("does-not-exist")


def test_duplicate_source_registration_raises():
    reg = _registry()
    reg.register_source(FakeIngestSource())
    with pytest.raises(ValueError):
        reg.register_source(FakeIngestSource())


def test_registry_is_isolated_per_instance():
    reg_a = _registry()
    reg_b = _registry()
    reg_a.register_source(FakeIngestSource())
    assert reg_a.list_sources()
    assert not reg_b.list_sources()
