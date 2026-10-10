# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""ForeignCredentialStore — named third-party credentials (issue #667).

Values live in a ``SecretStore`` (keychain in prod; injected in-memory
fake here — the real keychain is never touched by tests). Metadata
(names, provider kind, issuer URL, expiry, git wiring) lives in a
0600 JSON index in the state dir and NEVER contains values.
"""

from __future__ import annotations

import json
import os
import stat

import pytest

from axiom.extensions.builtins.secrets.foreign.store import (
    ForeignCredentialStore,
    declared_secret_names,
)
from axiom.extensions.builtins.secrets.providers.protocol import (
    Capabilities,
    Secret,
    SecretRef,
)


class InMemoryValueStore:
    """SecretStore-shaped fake for tests."""

    capabilities = Capabilities(read=True, write=True, delete=True)

    def __init__(self) -> None:
        self.values: dict[str, bytes] = {}

    def get(self, ref: SecretRef) -> Secret:
        if ref.path not in self.values:
            raise KeyError(ref.path)
        return Secret(value=self.values[ref.path], metadata={"backend": "fake"})

    def put(self, ref: SecretRef, value: bytes) -> None:
        self.values[ref.path] = value

    def delete(self, ref: SecretRef) -> None:
        if ref.path not in self.values:
            raise KeyError(ref.path)
        del self.values[ref.path]

    def list_paths(self, prefix: str) -> list[str]:
        return sorted(p for p in self.values if p.startswith(prefix))


@pytest.fixture
def value_store() -> InMemoryValueStore:
    return InMemoryValueStore()


@pytest.fixture
def store(tmp_path, value_store) -> ForeignCredentialStore:
    return ForeignCredentialStore(tmp_path, value_store=value_store)


class TestSetGetRemove:
    def test_set_stores_value_and_metadata(self, store, value_store):
        meta = store.set(
            "hpc-gitlab-pat", b"glpat-fake",
            provider="gitlab-pat",
            issuer_url="https://gitlab.example.org",
            expires_at="2026-08-01",
        )
        assert value_store.values["hpc-gitlab-pat"] == b"glpat-fake"
        assert meta["name"] == "hpc-gitlab-pat"
        assert meta["provider"] == "gitlab-pat"
        assert meta["issuer_url"] == "https://gitlab.example.org"
        assert meta["expires_at"] == "2026-08-01"
        assert meta["created_at"]

    def test_get_returns_secret(self, store):
        store.set("n", b"v")
        with store.get("n") as secret:
            assert secret.value == b"v"

    def test_get_missing_raises(self, store):
        with pytest.raises(KeyError):
            store.get("missing")

    def test_overwrite_keeps_created_at(self, store):
        first = store.set("n", b"v1")
        second = store.set("n", b"v2", expires_at="2027-01-01")
        assert second["created_at"] == first["created_at"]
        with store.get("n") as s:
            assert s.value == b"v2"

    def test_remove_deletes_value_and_metadata(self, store, value_store):
        store.set("n", b"v")
        store.remove("n")
        assert "n" not in value_store.values
        assert not store.exists("n")
        with pytest.raises(KeyError):
            store.remove("n")

    def test_rejects_bad_names(self, store):
        for bad in ("", "has space", "../escape", "a/b", "-leading"):
            with pytest.raises(ValueError):
                store.set(bad, b"v")


class TestMetadataIndex:
    def test_list_returns_metadata_only_no_values(self, store, tmp_path):
        store.set("a", b"SECRETVALA", provider="guided")
        store.set("b", b"SECRETVALB", expires_at="2026-09-01")
        rows = store.list()
        assert [r["name"] for r in rows] == ["a", "b"]
        blob = json.dumps(rows)
        assert "SECRETVALA" not in blob and "SECRETVALB" not in blob

    def test_index_file_never_contains_values(self, store, tmp_path):
        store.set("a", b"SUPERSECRETSENTINEL")
        index = tmp_path / "secrets" / "foreign-credentials.json"
        assert index.exists()
        assert "SUPERSECRETSENTINEL" not in index.read_text(encoding="utf-8")

    def test_index_file_mode_0600(self, store, tmp_path):
        store.set("a", b"v")
        index = tmp_path / "secrets" / "foreign-credentials.json"
        assert stat.S_IMODE(os.stat(index).st_mode) == 0o600

    def test_update_metadata(self, store):
        store.set("a", b"v")
        meta = store.update_metadata(
            "a", expires_at="2026-12-01", git_host="gitlab.example.org",
        )
        assert meta["expires_at"] == "2026-12-01"
        assert store.metadata("a")["git_host"] == "gitlab.example.org"

    def test_update_metadata_missing_raises(self, store):
        with pytest.raises(KeyError):
            store.update_metadata("nope", expires_at="2026-01-01")

    def test_declared_secret_names_reads_index_without_value_store(
        self, store, tmp_path
    ):
        store.set("a", b"v")
        store.set("b", b"w")
        assert declared_secret_names(tmp_path) == ["a", "b"]

    def test_declared_secret_names_empty_when_no_index(self, tmp_path):
        assert declared_secret_names(tmp_path / "fresh") == []
