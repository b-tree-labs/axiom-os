# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tests for ``SecretStoreRegistry`` — registration + lookup contract."""

from __future__ import annotations

import pytest

from axiom.extensions.builtins.secrets import (
    Capabilities,
    SecretStoreProvider,
    SecretStoreRegistry,
)


class _FakeProvider(SecretStoreProvider):
    kind = "fake"
    capabilities = Capabilities(read=True, write=True)
    _fingerprint_fields = ("endpoint",)

    def open(self):  # type: ignore[override]
        return None  # SEC-1: registry shape only; no runtime client yet

    def available(self) -> bool:  # type: ignore[override]
        return True


@pytest.fixture(autouse=True)
def _clean_registry():
    yield
    SecretStoreRegistry.unregister("fake")
    SecretStoreRegistry.unregister("fake-2")


def test_register_and_lookup():
    SecretStoreRegistry.register(_FakeProvider)
    assert "fake" in SecretStoreRegistry.available_kinds()
    assert SecretStoreRegistry.get("fake") is _FakeProvider


def test_create_instantiates_with_config():
    SecretStoreRegistry.register(_FakeProvider)
    inst = SecretStoreRegistry.create("fake", {"name": "primary", "endpoint": "x"})
    assert isinstance(inst, _FakeProvider)
    assert inst.name == "primary"
    assert inst.handles_sensitive_data is True
    assert inst.capabilities.write is True


def test_duplicate_kind_refused():
    SecretStoreRegistry.register(_FakeProvider)

    class _Other(SecretStoreProvider):
        kind = "fake"
        capabilities = Capabilities()

        def open(self):  # type: ignore[override]
            return None

    with pytest.raises(ValueError, match="already registered"):
        SecretStoreRegistry.register(_Other)


def test_re_register_same_class_is_noop():
    SecretStoreRegistry.register(_FakeProvider)
    SecretStoreRegistry.register(_FakeProvider)  # must not raise


def test_unknown_kind_raises_with_helpful_message():
    with pytest.raises(KeyError, match="No SecretStoreProvider registered"):
        SecretStoreRegistry.get("does-not-exist")


def test_empty_kind_rejected():
    class _Nameless(SecretStoreProvider):
        kind = ""
        capabilities = Capabilities()

        def open(self):  # type: ignore[override]
            return None

    with pytest.raises(ValueError, match="non-empty `kind`"):
        SecretStoreRegistry.register(_Nameless)
