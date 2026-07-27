# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``keychain`` SecretStoreProvider — darwin ``security``-CLI backend.

Unit layer drives the store through an injected fake runner — the real
macOS keychain is NEVER touched by default. The integration layer
(``TestDarwinKeychainIntegration``) is skip-by-default: it only runs when
``AXIOM_KEYCHAIN_IT=1`` on a Darwin host, and uses a dedicated throwaway
``axiom-test-*`` service namespace it creates and deletes itself.
"""

from __future__ import annotations

import os
import platform
import uuid

import pytest

from axiom.extensions.builtins.secrets.providers.keychain import (
    KeychainSecretStoreProvider,
    KeychainUnavailable,
)
from axiom.extensions.builtins.secrets.providers.protocol import SecretRef
from axiom.extensions.builtins.secrets.providers.registry import (
    SecretStoreRegistry,
)


class FakeSecurityRunner:
    """Stands in for the ``security`` CLI. Records calls; in-memory items."""

    def __init__(self) -> None:
        self.items: dict[tuple[str, str], str] = {}
        self.calls: list[list[str]] = []

    def __call__(self, args, *, input=None):  # noqa: ANN001
        self.calls.append(list(args))
        if args[0] == "find-generic-password":
            svc, acct = args[args.index("-s") + 1], args[args.index("-a") + 1]
            if (svc, acct) not in self.items:
                return 44, "", "security: SecKeychainSearchCopyNext"
            return 0, self.items[(svc, acct)] + "\n", ""
        if args[0] == "add-generic-password":
            svc, acct = args[args.index("-s") + 1], args[args.index("-a") + 1]
            value = args[args.index("-w") + 1]
            self.items[(svc, acct)] = value
            return 0, "", ""
        if args[0] == "delete-generic-password":
            svc, acct = args[args.index("-s") + 1], args[args.index("-a") + 1]
            if (svc, acct) not in self.items:
                return 44, "", "not found"
            del self.items[(svc, acct)]
            return 0, "", ""
        return 1, "", f"unknown: {args!r}"


@pytest.fixture
def runner() -> FakeSecurityRunner:
    return FakeSecurityRunner()


@pytest.fixture
def store(runner):
    provider = KeychainSecretStoreProvider(
        {"name": "test-keychain", "service": "axiom-secrets", "runner": runner}
    )
    return provider.open()


class TestKeychainStore:
    def test_registered_kind(self):
        assert "keychain" in SecretStoreRegistry.available_kinds()

    def test_put_then_get_roundtrip(self, store):
        ref = SecretRef.parse("keychain://hpc-gitlab-pat")
        store.put(ref, b"glpat-not-a-real-token")
        secret = store.get(ref)
        assert secret.value == b"glpat-not-a-real-token"
        assert secret.metadata["backend"] == "keychain"

    def test_get_missing_raises_keyerror(self, store):
        with pytest.raises(KeyError):
            store.get(SecretRef.parse("keychain://nope"))

    def test_put_overwrites_existing(self, store):
        ref = SecretRef.parse("keychain://n1")
        store.put(ref, b"old")
        store.put(ref, b"new")
        assert store.get(ref).value == b"new"

    def test_delete_removes(self, store):
        ref = SecretRef.parse("keychain://n1")
        store.put(ref, b"v")
        store.delete(ref)
        with pytest.raises(KeyError):
            store.get(ref)

    def test_delete_missing_raises_keyerror(self, store):
        with pytest.raises(KeyError):
            store.delete(SecretRef.parse("keychain://nope"))

    def test_items_scoped_to_service(self, runner):
        p1 = KeychainSecretStoreProvider(
            {"name": "a", "service": "axiom-secrets", "runner": runner}
        )
        s1 = p1.open()
        s1.put(SecretRef.parse("keychain://n"), b"v")
        assert ("axiom-secrets", "n") in runner.items

    def test_list_paths_refuses(self, store):
        # Enumeration comes from the metadata index, never the keychain.
        with pytest.raises(PermissionError):
            store.list_paths("")

    def test_capabilities_shape(self):
        caps = KeychainSecretStoreProvider.capabilities
        assert caps.read and caps.write and caps.delete
        assert caps.encryption_at_rest is True
        assert caps.list_paths is False


class TestGracefulUnavailability:
    def test_open_without_runner_off_darwin_raises_capability_error(
        self, monkeypatch
    ):
        monkeypatch.setattr(platform, "system", lambda: "Linux")
        provider = KeychainSecretStoreProvider({"name": "kc"})
        assert provider.available() is False
        with pytest.raises(KeychainUnavailable) as exc:
            provider.open()
        assert "keychain" in str(exc.value).lower()

    def test_available_true_with_injected_runner(self, runner):
        provider = KeychainSecretStoreProvider({"name": "kc", "runner": runner})
        assert provider.available() is True


# ---------------------------------------------------------------------------
# Integration — real `security` CLI, throwaway namespace. Skip-by-default.
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    os.environ.get("AXIOM_KEYCHAIN_IT") != "1" or platform.system() != "Darwin",
    reason=(
        "darwin keychain integration test — opt in with AXIOM_KEYCHAIN_IT=1 "
        "on macOS; uses a throwaway axiom-test-* namespace"
    ),
)
class TestDarwinKeychainIntegration:
    def test_roundtrip_against_real_security_cli(self):
        service = f"axiom-test-{uuid.uuid4().hex[:12]}"
        provider = KeychainSecretStoreProvider(
            {"name": "it-keychain", "service": service}
        )
        store = provider.open()
        ref = SecretRef.parse("keychain://axiom-test-item")
        try:
            store.put(ref, b"integration-test-value")
            assert store.get(ref).value == b"integration-test-value"
        finally:
            try:
                store.delete(ref)
            except KeyError:
                pass
        with pytest.raises(KeyError):
            store.get(ref)
