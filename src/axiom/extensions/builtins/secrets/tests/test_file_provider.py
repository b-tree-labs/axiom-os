# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``file`` SecretStoreProvider — 0600 JSON file, dev/CI fallback.

Exists so the foreign-credential surface works on hosts without an OS
keychain (Linux CI). Plaintext-at-rest is advertised
(``encryption_at_rest=False``) and the provider mirrors the ``env``
provider's loud warning outside ``AXIOM_MODE=dev``.
"""

from __future__ import annotations

import json
import os
import stat

import pytest

from axiom.extensions.builtins.secrets.providers.localfile import (
    FileSecretStoreProvider,
)
from axiom.extensions.builtins.secrets.providers.protocol import SecretRef
from axiom.extensions.builtins.secrets.providers.registry import (
    SecretStoreRegistry,
)


@pytest.fixture
def store(tmp_path):
    provider = FileSecretStoreProvider(
        {"name": "test-file", "path": str(tmp_path / "values.json")}
    )
    return provider.open()


class TestFileStore:
    def test_registered_kind(self):
        assert "file" in SecretStoreRegistry.available_kinds()

    def test_roundtrip(self, store):
        ref = SecretRef.parse("file://webhook-hmac")
        store.put(ref, b"hmac-key-bytes")
        assert store.get(ref).value == b"hmac-key-bytes"

    def test_get_missing_raises(self, store):
        with pytest.raises(KeyError):
            store.get(SecretRef.parse("file://nope"))

    def test_delete(self, store):
        ref = SecretRef.parse("file://x")
        store.put(ref, b"v")
        store.delete(ref)
        with pytest.raises(KeyError):
            store.get(ref)

    def test_file_mode_is_0600(self, tmp_path):
        path = tmp_path / "values.json"
        provider = FileSecretStoreProvider({"name": "f", "path": str(path)})
        provider.open().put(SecretRef.parse("file://x"), b"v")
        mode = stat.S_IMODE(os.stat(path).st_mode)
        assert mode == 0o600

    def test_values_not_stored_as_raw_plaintext_json_strings(self, tmp_path):
        # base64 at minimum — grep-for-the-token must not hit.
        path = tmp_path / "values.json"
        provider = FileSecretStoreProvider({"name": "f", "path": str(path)})
        provider.open().put(SecretRef.parse("file://x"), b"glpat-SENTINEL")
        raw = path.read_text(encoding="utf-8")
        assert "glpat-SENTINEL" not in raw
        json.loads(raw)  # still valid JSON

    def test_advertises_plaintext_at_rest(self):
        assert FileSecretStoreProvider.capabilities.encryption_at_rest is False

    def test_available_always(self, tmp_path):
        provider = FileSecretStoreProvider(
            {"name": "f", "path": str(tmp_path / "v.json")}
        )
        assert provider.available() is True
