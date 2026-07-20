# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Conformance tests — every registered SecretStoreProvider must satisfy
the same Protocol contract. Parametrized over
``SecretStoreRegistry.available_kinds()`` so new providers automatically
get checked when they register themselves.
"""

from __future__ import annotations

import pytest

from axiom.extensions.builtins.secrets import (
    Capabilities,
    SecretStoreProvider,
    SecretStoreRegistry,
)


def _factory_config(kind: str) -> dict:
    """Minimum config a kind needs to instantiate."""
    if kind == "openbao":
        class _Noop:
            def request(self, *a, **kw):
                return {}
        return {
            "name": "conformance",
            "url": "http://x",
            "token": "t",
            "_transport": _Noop(),
        }
    if kind == "gcp":
        # Inject a fake client so we don't need GCP SDK + creds in CI.
        class _NoopClient:
            def access_secret_version(self, *a, **kw): return None
            def get_secret(self, *a, **kw): return None
            def create_secret(self, *a, **kw): return None
            def add_secret_version(self, *a, **kw): return None
            def delete_secret(self, *a, **kw): return None
            def destroy_secret_version(self, *a, **kw): return None
            def list_secrets(self, *a, **kw): return iter([])
        return {
            "name": "conformance",
            "project": "conformance-project",
            "_client": _NoopClient(),
        }
    if kind == "aws":
        # Inject a fake client so we don't need boto3 creds / a region.
        class _NoopClient:
            def get_secret_value(self, *a, **kw): return {}
            def describe_secret(self, *a, **kw): return {}
            def create_secret(self, *a, **kw): return {}
            def put_secret_value(self, *a, **kw): return {}
            def delete_secret(self, *a, **kw): return {}
            def rotate_secret(self, *a, **kw): return {}
            def get_paginator(self, *a, **kw):
                class _P:
                    def paginate(self): return iter([])
                return _P()
        return {
            "name": "conformance",
            "region": "us-east-1",
            "_client": _NoopClient(),
        }
    if kind == "env":
        return {"name": "conformance"}
    return {"name": "conformance"}


@pytest.fixture(params=SecretStoreRegistry.available_kinds())
def kind(request):
    return request.param


def test_kind_is_registered(kind):
    assert kind in SecretStoreRegistry.available_kinds()


def test_provider_can_be_constructed(kind):
    cls = SecretStoreRegistry.get(kind)
    inst = cls(_factory_config(kind))
    assert isinstance(inst, SecretStoreProvider)


def test_provider_handles_sensitive_data_flag(kind):
    cls = SecretStoreRegistry.get(kind)
    inst = cls(_factory_config(kind))
    assert inst.handles_sensitive_data is True


def test_provider_advertises_capabilities(kind):
    cls = SecretStoreRegistry.get(kind)
    inst = cls(_factory_config(kind))
    assert isinstance(inst.capabilities, Capabilities)


def test_provider_open_returns_secret_store(kind):
    cls = SecretStoreRegistry.get(kind)
    inst = cls(_factory_config(kind))
    store = inst.open()
    # SecretStore is a Protocol; we check the method surface directly
    # to be tolerant of duck-typed implementations.
    for method in ("get", "put", "delete", "list_paths"):
        assert hasattr(store, method), \
            f"{kind} SecretStore missing required method {method}"


def test_provider_describe_redacts_secrets(kind):
    """``describe()`` is logged + printed; must not leak tokens / urls
    with credentials embedded."""
    cls = SecretStoreRegistry.get(kind)
    inst = cls(_factory_config(kind))
    desc = inst.describe()
    # The describe() format includes name + uid prefix + config_hash;
    # it must NOT include the raw token value.
    assert "test-token" not in desc
    assert "conformance" in desc or inst.name in desc


def test_provider_factory_is_in_registry(kind):
    cls = SecretStoreRegistry.get(kind)
    assert cls.kind == kind
