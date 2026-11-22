# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tests for the kubernetes SecretStoreProvider — uses a fake API so
the suite runs without a kubectl/kubeconfig present."""

from __future__ import annotations

import base64
import json
import types
from contextlib import contextmanager
from unittest.mock import MagicMock

import pytest

from axiom.extensions.builtins.secrets import (
    KubernetesSecretStoreProvider,
    SecretRef,
)
from axiom.extensions.builtins.secrets.providers.kubernetes import (
    _KubernetesSecretStore,
)


@contextmanager
def _patched_api(api: MagicMock):
    """Bind a MagicMock CoreV1Api into a freshly-built KubernetesSecretStore."""

    def _build(kube_context=None, in_cluster=False):
        store = _KubernetesSecretStore(
            kube_context=kube_context, in_cluster=in_cluster
        )
        store._api = api  # short-circuit _ensure_api()
        return store

    yield _build


def _mk_secret(name: str, ns: str, data: dict[str, str]):
    """Build a fake V1Secret-shaped object with b64-encoded data."""
    encoded = {
        k: base64.b64encode(v.encode("utf-8")).decode("ascii")
        for k, v in data.items()
    }
    s = types.SimpleNamespace(
        data=encoded,
        metadata=types.SimpleNamespace(name=name, resource_version="42"),
    )
    return s


# ---------------------------------------------------------------------------
# get()
# ---------------------------------------------------------------------------


class TestGet:
    def test_get_single_key(self):
        api = MagicMock()
        api.read_namespaced_secret.return_value = _mk_secret(
            "dp1-db", "axiom-data", {"password": "hunter2", "user": "axiom"}
        )
        with _patched_api(api) as build:
            store = build()
            s = store.get(SecretRef.parse(
                "kubernetes://axiom-data/dp1-db/password"
            ))
        assert s.value == b"hunter2"
        assert s.metadata["backend"] == "kubernetes"
        assert s.metadata["namespace"] == "axiom-data"
        assert s.metadata["key"] == "password"

    def test_get_without_key_returns_json_of_all_entries(self):
        api = MagicMock()
        api.read_namespaced_secret.return_value = _mk_secret(
            "dp1-db", "axiom-data", {"user": "axiom", "password": "p"}
        )
        with _patched_api(api) as build:
            s = build().get(SecretRef.parse("kubernetes://axiom-data/dp1-db"))
        payload = json.loads(s.value)
        assert payload == {"user": "axiom", "password": "p"}

    def test_get_missing_key_raises_keyerror(self):
        api = MagicMock()
        api.read_namespaced_secret.return_value = _mk_secret(
            "dp1-db", "axiom-data", {"user": "axiom"}
        )
        with _patched_api(api) as build:
            store = build()
            with pytest.raises(KeyError, match="no key 'password'"):
                store.get(SecretRef.parse(
                    "kubernetes://axiom-data/dp1-db/password"
                ))

    def test_get_404_translates_to_keyerror(self):
        api = MagicMock()
        api.read_namespaced_secret.side_effect = type(
            "ApiException", (Exception,), {"status": 404}
        )("not found")
        with _patched_api(api) as build:
            with pytest.raises(KeyError, match="no Secret"):
                build().get(SecretRef.parse(
                    "kubernetes://axiom-data/missing"
                ))

    def test_get_403_translates_to_permissionerror(self):
        api = MagicMock()
        api.read_namespaced_secret.side_effect = type(
            "ApiException", (Exception,), {"status": 403}
        )("forbidden")
        with _patched_api(api) as build:
            with pytest.raises(PermissionError, match="denied"):
                build().get(SecretRef.parse("kubernetes://axiom-data/dp1-db"))

    def test_ref_without_name_segment_is_clean_error(self):
        api = MagicMock()
        with _patched_api(api) as build:
            store = build()
            with pytest.raises(ValueError, match="ns/name"):
                store.get(SecretRef.parse("kubernetes://just-ns"))


# ---------------------------------------------------------------------------
# list_paths()
# ---------------------------------------------------------------------------


class TestList:
    def test_lists_all_secrets_in_namespace(self):
        api = MagicMock()
        api.list_namespaced_secret.return_value = types.SimpleNamespace(
            items=[
                types.SimpleNamespace(metadata=types.SimpleNamespace(name="a")),
                types.SimpleNamespace(metadata=types.SimpleNamespace(name="b")),
            ],
        )
        with _patched_api(api) as build:
            keys = build().list_paths("axiom-data")
        assert keys == ["axiom-data/a", "axiom-data/b"]

    def test_name_prefix_filters(self):
        api = MagicMock()
        api.list_namespaced_secret.return_value = types.SimpleNamespace(
            items=[
                types.SimpleNamespace(metadata=types.SimpleNamespace(name="dp1-db")),
                types.SimpleNamespace(metadata=types.SimpleNamespace(name="dp1-rag")),
                types.SimpleNamespace(metadata=types.SimpleNamespace(name="other")),
            ],
        )
        with _patched_api(api) as build:
            keys = build().list_paths("axiom-data/dp1-")
        assert keys == ["axiom-data/dp1-db", "axiom-data/dp1-rag"]


# ---------------------------------------------------------------------------
# Mutations are intentionally blocked in SEC-3
# ---------------------------------------------------------------------------


class TestReadOnly:
    def test_put_is_refused(self):
        api = MagicMock()
        with _patched_api(api) as build:
            with pytest.raises(PermissionError, match="read-only"):
                build().put(
                    SecretRef.parse("kubernetes://axiom-data/dp1-db"),
                    b"x",
                )

    def test_delete_is_refused(self):
        api = MagicMock()
        with _patched_api(api) as build:
            with pytest.raises(PermissionError, match="read-only"):
                build().delete(
                    SecretRef.parse("kubernetes://axiom-data/dp1-db")
                )


# ---------------------------------------------------------------------------
# Provider factory + capability advertisement
# ---------------------------------------------------------------------------


class TestProvider:
    def test_capabilities_advertise_read_only(self):
        caps = KubernetesSecretStoreProvider.capabilities
        assert caps.read is True
        assert caps.write is False
        assert caps.dynamic_credentials is False
        assert caps.rotation is False

    def test_available_true_when_kubernetes_importable(self):
        p = KubernetesSecretStoreProvider({"name": "test"})
        # kubernetes-client may or may not be installed in the test env;
        # the contract is that available() reflects importability without
        # raising.
        result = p.available()
        assert isinstance(result, bool)

    def test_factory_creates_store(self):
        p = KubernetesSecretStoreProvider({
            "name": "test", "kube_context": "k3d-example",
        })
        store = p.open()
        assert isinstance(store, _KubernetesSecretStore)
        assert store._kube_context == "k3d-example"
        assert store._in_cluster is False
