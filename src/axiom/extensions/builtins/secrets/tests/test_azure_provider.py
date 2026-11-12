# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``azure_key_vault`` provider — ref translation, roundtrip, error mapping.

The Azure SDK is not a test dependency; a ``FakeSecretClient`` stands in for
``azure.keyvault.secrets.SecretClient`` via the ``_client`` config seam, so
these tests exercise our translation layer (ref → vault/name, bytes ↔ str,
SDK error → KeyError/PermissionError) without a live vault or the wheel
installed.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from axiom.extensions.builtins.secrets.providers.azure_key_vault import (
    AzureKeyVaultProvider,
    _azure_status,
)
from axiom.extensions.builtins.secrets.providers.protocol import SecretRef


# --- Azure SDK stand-ins ----------------------------------------------------


class ResourceNotFoundError(Exception):
    """Mirrors azure.core.exceptions.ResourceNotFoundError (status 404)."""

    status_code = 404


class ClientAuthenticationError(Exception):
    """Mirrors azure.core.exceptions.ClientAuthenticationError (status 401)."""

    status_code = 401


class FakeSecretClient:
    """In-memory stand-in for one vault's SecretClient.

    One instance serves every vault name in-process (matching how the
    provider's ``_client`` seam injects a single client); vault scoping is
    the SDK's job in production, so the fake just keeps a flat name→value map.
    """

    def __init__(self) -> None:
        # name -> list of (version_id, value) newest last
        self._store: dict[str, list[tuple[str, str]]] = {}
        self.deleted: list[str] = []

    def set_secret(self, name: str, value: str):
        versions = self._store.setdefault(name, [])
        vid = f"v{len(versions) + 1}"
        versions.append((vid, value))
        return SimpleNamespace(properties=SimpleNamespace(version=vid))

    def get_secret(self, name: str, version=None):
        versions = self._store.get(name)
        if not versions:
            raise ResourceNotFoundError(f"secret {name} not found")
        if version in (None, ""):
            vid, value = versions[-1]
        else:
            match = [(v, val) for v, val in versions if v == version]
            if not match:
                raise ResourceNotFoundError(f"secret {name} version {version} not found")
            vid, value = match[0]
        return SimpleNamespace(
            value=value, properties=SimpleNamespace(version=vid, name=name)
        )

    def begin_delete_secret(self, name: str):
        if name not in self._store:
            raise ResourceNotFoundError(f"secret {name} not found")
        del self._store[name]
        self.deleted.append(name)
        return SimpleNamespace(result=lambda: SimpleNamespace(name=name))

    def list_properties_of_secrets(self):
        return [SimpleNamespace(name=n) for n in sorted(self._store)]


@pytest.fixture
def client() -> FakeSecretClient:
    return FakeSecretClient()


@pytest.fixture
def provider(client) -> AzureKeyVaultProvider:
    return AzureKeyVaultProvider({"name": "test-azure", "_client": client})


@pytest.fixture
def store(provider):
    return provider.open()


# --- ref translation --------------------------------------------------------


class TestRefTranslation:
    def test_splits_vault_and_name(self, store, client):
        client.set_secret("db-password", "hunter2")
        secret = store.get(SecretRef.parse("azure://prod-vault/db-password"))
        assert secret.value == b"hunter2"
        assert secret.metadata["vault"] == "prod-vault"
        assert secret.metadata["name"] == "db-password"

    def test_default_vault_when_ref_names_only_secret(self, client):
        client.set_secret("api-key", "abc")
        prov = AzureKeyVaultProvider(
            {"name": "t", "vault": "fallback-vault", "_client": client}
        )
        secret = prov.open().get(SecretRef.parse("azure://api-key"))
        assert secret.value == b"abc"
        assert secret.metadata["vault"] == "fallback-vault"

    def test_missing_vault_without_default_raises(self, store):
        with pytest.raises(ValueError, match="missing vault"):
            store.get(SecretRef.parse("azure://lonely-name"))


# --- roundtrip --------------------------------------------------------------


class TestRoundtrip:
    def test_put_then_get(self, store):
        ref = SecretRef.parse("azure://v/sendgrid-key")
        store.put(ref, b"SG.xxxxx")
        got = store.get(ref)
        assert got.value == b"SG.xxxxx"
        assert got.metadata["backend"] == "azure_key_vault"
        # Azure version ids are opaque strings, surfaced in metadata not `version`.
        assert got.version is None
        assert got.metadata["version_id"] == "v1"

    def test_put_creates_new_version_each_write(self, store, client):
        ref = SecretRef.parse("azure://v/rotating")
        store.put(ref, b"one")
        store.put(ref, b"two")
        latest = store.get(ref)
        assert latest.value == b"two"
        assert latest.metadata["version_id"] == "v2"
        pinned = store.get(SecretRef.parse("azure://v/rotating?version=v1"))
        assert pinned.value == b"one"

    def test_get_latest_ignores_literal_latest(self, store):
        ref = SecretRef.parse("azure://v/x")
        store.put(ref, b"payload")
        got = store.get(SecretRef.parse("azure://v/x?version=latest"))
        assert got.value == b"payload"


# --- errors -----------------------------------------------------------------


class TestErrors:
    def test_missing_secret_is_keyerror(self, store):
        with pytest.raises(KeyError):
            store.get(SecretRef.parse("azure://v/nope"))

    def test_auth_failure_is_permissionerror(self, provider):
        class Denying(FakeSecretClient):
            def get_secret(self, name, version=None):
                raise ClientAuthenticationError("401")

        prov = AzureKeyVaultProvider({"name": "t", "_client": Denying()})
        with pytest.raises(PermissionError):
            prov.open().get(SecretRef.parse("azure://v/x"))

    def test_status_extraction(self):
        assert _azure_status(ResourceNotFoundError()) == 404
        assert _azure_status(ClientAuthenticationError()) == 401
        assert _azure_status(RuntimeError("weird")) is None


# --- delete + list ----------------------------------------------------------


class TestDelete:
    def test_delete_removes_whole_secret(self, store, client):
        ref = SecretRef.parse("azure://v/temp")
        store.put(ref, b"data")
        store.delete(ref)
        assert "temp" in client.deleted
        with pytest.raises(KeyError):
            store.get(ref)

    def test_version_qualified_delete_still_removes_secret(self, store, client):
        # Documented divergence from GCP: Key Vault has no per-version destroy.
        ref = SecretRef.parse("azure://v/temp")
        store.put(ref, b"one")
        store.put(ref, b"two")
        store.delete(SecretRef.parse("azure://v/temp?version=v1"))
        assert "temp" in client.deleted

    def test_delete_missing_is_keyerror(self, store):
        with pytest.raises(KeyError):
            store.delete(SecretRef.parse("azure://v/ghost"))


class TestList:
    def test_list_paths_prefixes_vault(self, store):
        store.put(SecretRef.parse("azure://v/alpha"), b"1")
        store.put(SecretRef.parse("azure://v/beta"), b"2")
        store.put(SecretRef.parse("azure://v/alto"), b"3")
        assert store.list_paths("v") == ["v/alpha", "v/alto", "v/beta"]

    def test_list_paths_filters_by_name_prefix(self, store):
        store.put(SecretRef.parse("azure://v/alpha"), b"1")
        store.put(SecretRef.parse("azure://v/beta"), b"2")
        store.put(SecretRef.parse("azure://v/alto"), b"3")
        assert store.list_paths("v/al") == ["v/alpha", "v/alto"]

    def test_list_paths_needs_a_vault(self, store):
        with pytest.raises(ValueError, match="needs a vault"):
            store.list_paths("")


# --- provider surface -------------------------------------------------------


class TestProvider:
    def test_kind_and_capabilities(self):
        assert AzureKeyVaultProvider.kind == "azure"
        caps = AzureKeyVaultProvider.capabilities
        assert caps.read and caps.write and caps.delete and caps.list_paths
        assert caps.versions and caps.audit_stream and caps.encryption_at_rest
        assert not caps.dynamic_credentials
        assert not caps.rotation

    def test_registered_under_azure_scheme(self):
        from axiom.extensions.builtins.secrets.providers.registry import (
            SecretStoreRegistry,
        )

        assert SecretStoreRegistry.get("azure") is AzureKeyVaultProvider

    def test_default_config_carries_azure_vault(self, monkeypatch):
        from axiom.extensions.builtins import secrets

        monkeypatch.setenv("AXIOM_AZURE_VAULT", "cfg-vault")
        cfg = secrets._default_config_for_scheme("azure")
        assert cfg["vault"] == "cfg-vault"


class TestUnsupportedOps:
    def test_lease_refused(self, store):
        with pytest.raises(PermissionError):
            store.lease(SecretRef.parse("azure://v/x"), 60)

    def test_rotate_refused(self, store):
        with pytest.raises(PermissionError):
            store.rotate(SecretRef.parse("azure://v/x"))
