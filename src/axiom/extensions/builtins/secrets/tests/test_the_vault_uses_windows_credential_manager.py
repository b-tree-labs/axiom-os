# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""On Windows the vault keeps values in Credential Manager, not a file.

The ``keychain`` provider drove the macOS ``security`` CLI and nothing else,
so on Windows it reported no keychain: ``secrets set`` fell back to a
plaintext-equivalent file in dev mode and refused outside it, and a
``keychain://`` reference in a site's config could not be resolved at all. A
Windows host has an OS credential store; this uses it through ``keyring``.
"""

from __future__ import annotations

import sys

import pytest

keyring = pytest.importorskip("keyring")
from keyring.backend import KeyringBackend  # noqa: E402

from axiom.extensions.builtins.secrets.providers.keychain import (  # noqa: E402
    KeychainSecretStoreProvider,
    KeychainUnavailable,
)
from axiom.extensions.builtins.secrets.providers.protocol import SecretRef  # noqa: E402


class _Memory(KeyringBackend):
    priority = 1

    def __init__(self):
        super().__init__()
        self.items: dict[tuple[str, str], str] = {}

    def get_password(self, service, username):
        return self.items.get((service, username))

    def set_password(self, service, username, password):
        self.items[(service, username)] = password

    def delete_password(self, service, username):
        from keyring.errors import PasswordDeleteError

        if (service, username) not in self.items:
            raise PasswordDeleteError(username)
        del self.items[(service, username)]


def _ref(name):
    return SecretRef.parse(f"keychain://{name}")


@pytest.fixture
def memory(monkeypatch):
    backend = _Memory()
    monkeypatch.setattr("platform.system", lambda: "Windows")
    previous = keyring.get_keyring()
    keyring.set_keyring(backend)
    yield backend
    keyring.set_keyring(previous)


def test_on_windows_a_value_round_trips_through_the_os_credential_store(memory):
    store = KeychainSecretStoreProvider({"service": "axiom-test"}).open()
    store.put(_ref("site-ingest"), b"value-1")
    assert store.get(_ref("site-ingest")).value == b"value-1"
    assert memory.items[("axiom-test", "site-ingest")] == "value-1"
    store.put(_ref("site-ingest"), b"value-2")
    assert store.get(_ref("site-ingest")).value == b"value-2"
    store.delete(_ref("site-ingest"))
    with pytest.raises(KeyError):
        store.get(_ref("site-ingest"))


def test_a_missing_item_is_a_key_error_like_the_mac_backend(memory):
    store = KeychainSecretStoreProvider({}).open()
    with pytest.raises(KeyError):
        store.get(_ref("nothing-here"))


def test_a_plaintext_keyring_backend_is_not_a_vault(monkeypatch):
    """keyring will happily fall back to a plaintext file. That is the very
    thing this replaces, so it does not count as available."""
    from keyring.backends import fail

    monkeypatch.setattr("platform.system", lambda: "Windows")
    previous = keyring.get_keyring()
    keyring.set_keyring(fail.Keyring())
    try:
        provider = KeychainSecretStoreProvider({})
        assert not provider.available()
        with pytest.raises(KeychainUnavailable):
            provider.open()
    finally:
        keyring.set_keyring(previous)


@pytest.mark.skipif(sys.platform != "win32", reason="the real Credential Manager is Windows-only")
def test_the_real_windows_credential_manager():
    """Run on the windows-latest CI leg: the provider against the OS store."""
    import uuid

    name = f"axiom-test-{uuid.uuid4().hex[:8]}"
    store = KeychainSecretStoreProvider({"service": "axiom-test"}).open()
    try:
        store.put(_ref(name), b"wincred-value")
        assert store.get(_ref(name)).value == b"wincred-value"
        # keyring's default here is a chain; what it chains must include the
        # Windows Credential Manager backend.
        backend = keyring.get_keyring()
        names = {type(b).__name__ for b in getattr(backend, "backends", [backend])}
        assert "WinVaultKeyring" in names, names
    finally:
        try:
            store.delete(_ref(name))
        except KeyError:
            pass


def test_a_chain_holding_a_real_store_counts(monkeypatch, memory):
    """keyring's default on a real Windows host is a ChainerBackend wrapping
    WinVaultKeyring. Treating "chainer" as not-a-vault refused the very store
    this exists to use (found on windows-latest)."""
    from keyring.backends.chainer import ChainerBackend

    from axiom.extensions.builtins.secrets.providers import keychain

    chain = ChainerBackend()
    monkeypatch.setattr(type(chain), "backends", property(lambda self: [memory]))
    keyring.set_keyring(chain)
    assert keychain._windows_store_ready()


def test_a_chain_of_only_non_stores_does_not_count(monkeypatch):
    from keyring.backends import fail
    from keyring.backends.chainer import ChainerBackend

    from axiom.extensions.builtins.secrets.providers import keychain

    chain = ChainerBackend()
    monkeypatch.setattr(type(chain), "backends", property(lambda self: [fail.Keyring()]))
    previous = keyring.get_keyring()
    keyring.set_keyring(chain)
    try:
        assert not keychain._windows_store_ready()
    finally:
        keyring.set_keyring(previous)
