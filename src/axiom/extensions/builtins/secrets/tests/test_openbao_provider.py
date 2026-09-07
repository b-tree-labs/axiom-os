# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tests for the openbao SecretStoreProvider — uses a fake transport so
the suite runs without a live OpenBao instance."""

from __future__ import annotations

from typing import Any

import pytest

from axiom.extensions.builtins.secrets import (
    OpenBaoSecretStoreProvider,
    SecretRef,
)


class FakeTransport:
    """Minimal stand-in for the urllib-based ``_BaoTransport``."""

    def __init__(self, store: dict[str, dict[str, Any]] | None = None) -> None:
        self.store = store or {}
        self.calls: list[tuple[str, str, Any, Any]] = []

    def request(
        self, method: str, path: str, *, body: Any = None,
        query: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        self.calls.append((method, path, body, query))
        if method == "GET":
            if path in self.store:
                return {"data": self.store[path]}
            raise KeyError(f"openbao 404 at {path}")
        if method == "POST":
            self.store[path] = body or {}
            return {"data": {"version": 1}}
        if method == "DELETE":
            self.store.pop(path, None)
            return {}
        if method == "LIST":
            prefix = path
            keys = sorted(
                k.removeprefix(prefix + "/").split("/", 1)[0]
                for k in self.store if k.startswith(prefix + "/")
            )
            return {"data": {"keys": keys}}
        return {}


@pytest.fixture
def transport():
    return FakeTransport()


@pytest.fixture
def provider(transport):
    return OpenBaoSecretStoreProvider({
        "name": "primary",
        "url": "http://bao.test:8200",
        "token": "test-token",
        "_transport": transport,
    })


def test_required_url_validates():
    with pytest.raises(ValueError, match="missing required"):
        OpenBaoSecretStoreProvider({"name": "x", "token": "t"})


def test_token_required(monkeypatch):
    monkeypatch.delenv("AXIOM_OPENBAO_TOKEN", raising=False)
    with pytest.raises(ValueError, match="requires a token"):
        OpenBaoSecretStoreProvider({"name": "x", "url": "http://x"})


def test_token_from_env(monkeypatch):
    monkeypatch.setenv("AXIOM_OPENBAO_TOKEN", "env-token")
    p = OpenBaoSecretStoreProvider({"name": "x", "url": "http://x"})
    assert p._token == "env-token"


def test_put_then_get_roundtrip(provider, transport):
    ref = SecretRef.parse("openbao://kv/data/example-host/db/password")
    provider.open().put(ref, b"hunter2")
    s = provider.open().get(ref)
    assert s.value == b"hunter2"
    assert s.metadata["backend"] == "openbao"


def test_get_returns_structured_payload_when_no_value_key(provider, transport):
    transport.store["kv/data/structured"] = {
        "data": {"username": "u", "password": "p"},
        "metadata": {"version": 3},
    }
    s = provider.open().get(SecretRef.parse("openbao://kv/data/structured"))
    import json
    payload = json.loads(s.value)
    assert payload == {"username": "u", "password": "p"}
    assert s.version == 3


def test_get_missing_raises_keyerror(provider):
    with pytest.raises(KeyError):
        provider.open().get(SecretRef.parse("openbao://kv/data/missing"))


def test_versioned_read_sends_version_query(provider, transport):
    transport.store["kv/data/foo"] = {
        "data": {"value": "v3"},
        "metadata": {"version": 3},
    }
    provider.open().get(SecretRef.parse("openbao://kv/data/foo?version=3"))
    method, path, body, query = transport.calls[-1]
    assert query == {"version": "3"}


def test_delete_removes_from_store(provider, transport):
    transport.store["kv/data/foo"] = {"data": {"value": "v"}}
    provider.open().delete(SecretRef.parse("openbao://kv/data/foo"))
    assert "kv/data/foo" not in transport.store


def test_list_paths_translates_data_to_metadata(provider, transport):
    transport.store["kv/metadata/team/a"] = {"data": {"value": "v"}}
    transport.store["kv/metadata/team/b"] = {"data": {"value": "v"}}
    keys = provider.open().list_paths("kv/data/team")
    # The kv/v2 LIST uses /metadata/, store has team/a and team/b under metadata.
    assert "a" in keys and "b" in keys


def test_available_probes_health(provider, transport):
    # Stage a successful health endpoint.
    transport.store["sys/health"] = {"sealed": False}
    assert provider.available() is True


def test_available_false_on_unreachable():
    bad_transport = FakeTransport()
    # FakeTransport has no sys/health, so request raises KeyError → available=False.
    p = OpenBaoSecretStoreProvider({
        "name": "down", "url": "http://x", "token": "t",
        "_transport": bad_transport,
    })
    assert p.available() is False


def test_capabilities_advertise_kv_v2():
    caps = OpenBaoSecretStoreProvider.capabilities
    assert caps.read and caps.write and caps.delete
    assert caps.versions and caps.encryption_at_rest and caps.audit_stream
    assert caps.dynamic_credentials is False  # transit/db land in SEC-6
    assert caps.rotation is False
