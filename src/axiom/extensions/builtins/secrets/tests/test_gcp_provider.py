# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tests for the gcp_secret_manager SecretStoreProvider — uses a fake
SDK client so the suite runs without GCP credentials present."""

from __future__ import annotations

import types

import pytest

from axiom.extensions.builtins.secrets import (
    GCPSecretManagerProvider,
    SecretRef,
)
from axiom.extensions.builtins.secrets.providers.gcp_secret_manager import (
    _GCPSecretManagerStore,
    _sdk_status,
)


# ---------------------------------------------------------------------------
# Fake SDK client — mimics google.cloud.secretmanager_v1 shape
# ---------------------------------------------------------------------------


class FakeClient:
    """Stub of google.cloud.secretmanager_v1.SecretManagerServiceClient."""

    def __init__(self) -> None:
        # Storage keyed by secret resource ("projects/p/secrets/n") →
        # list of bytes-payload versions (1-indexed).
        self.secrets: dict[str, list[bytes]] = {}
        self.calls: list[tuple[str, dict]] = []

    # ---- access ---------------------------------------------------------

    def access_secret_version(self, name: str):
        self.calls.append(("access_secret_version", {"name": name}))
        parent, _, ver = name.rpartition("/versions/")
        versions = self.secrets.get(parent)
        if not versions:
            raise type("NotFound", (Exception,), {})(f"{name} not found")
        if ver == "latest":
            idx = len(versions) - 1
        else:
            try:
                idx = int(ver) - 1
            except ValueError as exc:
                raise type("InvalidArgument", (Exception,), {})(
                    f"bad version: {ver}"
                ) from exc
        if idx < 0 or idx >= len(versions):
            raise type("NotFound", (Exception,), {})(f"version {ver}")
        return types.SimpleNamespace(
            name=f"{parent}/versions/{idx + 1}",
            payload=types.SimpleNamespace(data=versions[idx]),
        )

    # ---- get / create / add_version ------------------------------------

    def get_secret(self, name: str):
        self.calls.append(("get_secret", {"name": name}))
        if name not in self.secrets:
            raise type("NotFound", (Exception,), {})(f"{name} not found")
        return types.SimpleNamespace(name=name)

    def create_secret(self, parent: str, secret_id: str, secret: dict):
        self.calls.append(("create_secret", {
            "parent": parent, "secret_id": secret_id, "secret": secret,
        }))
        full = f"{parent}/secrets/{secret_id}"
        if full not in self.secrets:
            self.secrets[full] = []
        return types.SimpleNamespace(name=full)

    def add_secret_version(self, parent: str, payload: dict):
        self.calls.append(("add_secret_version", {
            "parent": parent, "payload": payload,
        }))
        data = payload.get("data", b"")
        self.secrets.setdefault(parent, []).append(data)
        ver = len(self.secrets[parent])
        return types.SimpleNamespace(
            name=f"{parent}/versions/{ver}",
        )

    # ---- delete ---------------------------------------------------------

    def delete_secret(self, name: str):
        self.calls.append(("delete_secret", {"name": name}))
        if name not in self.secrets:
            raise type("NotFound", (Exception,), {})(f"{name} not found")
        self.secrets.pop(name)

    def destroy_secret_version(self, name: str):
        self.calls.append(("destroy_secret_version", {"name": name}))
        parent, _, ver = name.rpartition("/versions/")
        versions = self.secrets.get(parent)
        if not versions:
            raise type("NotFound", (Exception,), {})(f"{name} not found")
        try:
            idx = int(ver) - 1
        except ValueError as exc:
            raise type("InvalidArgument", (Exception,), {})(
                f"bad version: {ver}"
            ) from exc
        # Destroy zeros out the payload but keeps the position; matches
        # SM's actual semantics closely enough for our purposes.
        versions[idx] = b""

    # ---- list -----------------------------------------------------------

    def list_secrets(self, parent: str):
        self.calls.append(("list_secrets", {"parent": parent}))
        prefix = f"{parent}/secrets/"
        for full in sorted(self.secrets):
            if full.startswith(prefix):
                yield types.SimpleNamespace(name=full)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def client():
    return FakeClient()


@pytest.fixture
def provider(client):
    return GCPSecretManagerProvider({
        "name": "primary",
        "project": "test-project",
        "_client": client,
    })


# ---------------------------------------------------------------------------
# SecretRef resource translation
# ---------------------------------------------------------------------------


class TestResourceTranslation:
    def test_full_project_in_url(self, provider):
        store = provider.open()
        ref = SecretRef.parse("gcp://my-proj/db-password")
        assert store._resource(ref) == (
            "projects/my-proj/secrets/db-password/versions/latest"
        )

    def test_default_project_used_when_omitted(self, provider):
        store = provider.open()
        # Note: SecretRef.parse splits on first /; we expect callers to
        # use gcp://<project>/<name>, but if they pass just a name with
        # a default project configured it should resolve.
        ref = SecretRef(scheme="gcp", path="db-password")
        assert store._resource(ref) == (
            "projects/test-project/secrets/db-password/versions/latest"
        )

    def test_version_query_param_threaded(self, provider):
        store = provider.open()
        ref = SecretRef.parse("gcp://my-proj/db-password?version=3")
        assert store._resource(ref).endswith("/versions/3")

    def test_missing_project_raises(self, client):
        p = GCPSecretManagerProvider({
            "name": "p", "_client": client,  # no project configured
        })
        store = p.open()
        ref = SecretRef(scheme="gcp", path="lonely")
        with pytest.raises(ValueError, match="project"):
            store._resource(ref)


# ---------------------------------------------------------------------------
# Roundtrip
# ---------------------------------------------------------------------------


class TestRoundtrip:
    def test_put_then_get(self, provider, client):
        ref = SecretRef.parse("gcp://test-project/db-password")
        provider.open().put(ref, b"hunter2")
        s = provider.open().get(ref)
        assert s.value == b"hunter2"
        assert s.metadata["backend"] == "gcp_secret_manager"
        assert s.version == 1

    def test_multiple_versions(self, provider):
        ref = SecretRef.parse("gcp://test-project/rotating")
        provider.open().put(ref, b"v1")
        provider.open().put(ref, b"v2")
        provider.open().put(ref, b"v3")

        # latest
        assert provider.open().get(ref).value == b"v3"
        assert provider.open().get(ref).version == 3
        # specific
        v2 = SecretRef.parse("gcp://test-project/rotating?version=2")
        assert provider.open().get(v2).value == b"v2"
        assert provider.open().get(v2).version == 2

    def test_put_creates_secret_when_missing(self, provider, client):
        ref = SecretRef.parse("gcp://test-project/brand-new")
        provider.open().put(ref, b"first")
        # Both create_secret + add_secret_version should have fired.
        ops = [c[0] for c in client.calls]
        assert "create_secret" in ops
        assert "add_secret_version" in ops

    def test_put_skips_create_when_secret_exists(self, provider, client):
        ref = SecretRef.parse("gcp://test-project/existing")
        provider.open().put(ref, b"v1")
        client.calls.clear()
        provider.open().put(ref, b"v2")
        ops = [c[0] for c in client.calls]
        assert "create_secret" not in ops
        assert "add_secret_version" in ops


# ---------------------------------------------------------------------------
# Error translation
# ---------------------------------------------------------------------------


class TestErrors:
    def test_get_missing_translates_to_keyerror(self, provider):
        with pytest.raises(KeyError):
            provider.open().get(SecretRef.parse(
                "gcp://test-project/never-existed"
            ))

    def test_get_403_translates_to_permissionerror(self, provider, client):
        def forbid(*a, **kw):
            exc = type("PermissionDenied", (Exception,), {})("nope")
            raise exc
        client.access_secret_version = forbid  # type: ignore[assignment]
        with pytest.raises(PermissionError):
            provider.open().get(SecretRef.parse(
                "gcp://test-project/anything"
            ))

    def test_sdk_status_recognizes_named_exceptions(self):
        class NotFound(Exception):
            pass

        class PermissionDenied(Exception):
            pass

        assert _sdk_status(NotFound("x")) == 404
        assert _sdk_status(PermissionDenied("x")) == 403

    def test_sdk_status_uses_int_code_attr(self):
        exc = Exception("x")
        exc.code = 503  # type: ignore[attr-defined]
        assert _sdk_status(exc) == 503


# ---------------------------------------------------------------------------
# Delete
# ---------------------------------------------------------------------------


class TestDelete:
    def test_delete_without_version_removes_whole_secret(self, provider, client):
        ref = SecretRef.parse("gcp://test-project/doomed")
        provider.open().put(ref, b"v1")
        provider.open().delete(ref)
        with pytest.raises(KeyError):
            provider.open().get(ref)
        assert any(c[0] == "delete_secret" for c in client.calls)

    def test_delete_with_version_destroys_just_that_version(
        self, provider, client
    ):
        ref = SecretRef.parse("gcp://test-project/rolling")
        provider.open().put(ref, b"v1")
        provider.open().put(ref, b"v2")
        v1 = SecretRef.parse("gcp://test-project/rolling?version=1")
        provider.open().delete(v1)
        # v2 still readable
        latest = provider.open().get(ref)
        assert latest.value == b"v2"
        assert any(c[0] == "destroy_secret_version" for c in client.calls)


# ---------------------------------------------------------------------------
# List
# ---------------------------------------------------------------------------


class TestList:
    def test_list_all_secrets_in_project(self, provider):
        provider.open().put(SecretRef.parse("gcp://test-project/a"), b"x")
        provider.open().put(SecretRef.parse("gcp://test-project/b"), b"x")
        provider.open().put(SecretRef.parse("gcp://test-project/db-pwd"), b"x")
        names = provider.open().list_paths("test-project")
        assert names == ["test-project/a", "test-project/b", "test-project/db-pwd"]

    def test_list_with_name_prefix(self, provider):
        provider.open().put(SecretRef.parse("gcp://test-project/dp1-a"), b"x")
        provider.open().put(SecretRef.parse("gcp://test-project/dp1-b"), b"x")
        provider.open().put(SecretRef.parse("gcp://test-project/other"), b"x")
        names = provider.open().list_paths("test-project/dp1-")
        assert names == ["test-project/dp1-a", "test-project/dp1-b"]


# ---------------------------------------------------------------------------
# Provider factory + advertisement
# ---------------------------------------------------------------------------


class TestProvider:
    def test_capabilities_advertise_gcp_shape(self):
        caps = GCPSecretManagerProvider.capabilities
        assert caps.read and caps.write and caps.delete and caps.versions
        assert caps.encryption_at_rest and caps.audit_stream
        assert caps.dynamic_credentials is False
        assert caps.rotation is False

    def test_available_iff_sdk_importable(self, client):
        p = GCPSecretManagerProvider({
            "name": "p", "project": "x", "_client": client,
        })
        # Returns a bool either way; suite may or may not have the SDK.
        assert isinstance(p.available(), bool)

    def test_factory_creates_store_with_default_project(self, client):
        p = GCPSecretManagerProvider({
            "name": "p", "project": "explicit-proj", "_client": client,
        })
        store = p.open()
        assert isinstance(store, _GCPSecretManagerStore)
        assert store._default_project == "explicit-proj"


# ---------------------------------------------------------------------------
# Lease/rotate refused (consistent with capabilities)
# ---------------------------------------------------------------------------


class TestUnsupportedOps:
    def test_lease_refused(self, provider):
        with pytest.raises(PermissionError, match="leased"):
            provider.open().lease(
                SecretRef.parse("gcp://test-project/x"), 60,
            )

    def test_rotate_refused(self, provider):
        with pytest.raises(PermissionError, match="rotation"):
            provider.open().rotate(SecretRef.parse("gcp://test-project/x"))
