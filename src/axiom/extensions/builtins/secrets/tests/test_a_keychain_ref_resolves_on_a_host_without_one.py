# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""A ``keychain://`` reference resolves from the vault ``secrets set`` wrote,
on a host that has no OS keychain.

Found in the 2026-10-07 partner dress rehearsal on a Linux DAQ host: the site's
config says ``token_ref = "keychain://<site>-ingest"``, ``secrets set`` stored
the key in the vault (the 0600 file, since Linux has no keychain), and the
reference refused to resolve: "no usable OS keychain". One ``site.toml`` has to
work on macOS, Windows and Linux, so on a host without an OS keychain the
reference reads the same store the vault writes, by the same rules: an explicit
backend, the dev-mode file, or a clear refusal outside dev.
"""

from __future__ import annotations

import pytest

from axiom.extensions.builtins.secrets import resolve
from axiom.extensions.builtins.secrets.foreign.store import open_default_value_store
from axiom.extensions.builtins.secrets.providers import keychain as kc
from axiom.extensions.builtins.secrets.providers.protocol import SecretRef


@pytest.fixture
def linux_host(tmp_path, monkeypatch):
    monkeypatch.setattr(kc.platform, "system", lambda: "Linux")
    monkeypatch.setenv("AXI_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("AXIOM_MODE", "dev")
    monkeypatch.delenv("AXIOM_FOREIGN_SECRETS_BACKEND", raising=False)
    return tmp_path


def _store_like_secrets_set(state_dir, name, value):
    open_default_value_store(state_dir).put(SecretRef.parse(f"file://{name}"), value.encode())


def test_a_keychain_ref_reads_what_the_vault_wrote(linux_host):
    _store_like_secrets_set(linux_host, "site-ingest", "s3cret-value")
    secret = resolve(SecretRef.parse("keychain://site-ingest"))
    assert getattr(secret, "value", secret) in (b"s3cret-value", "s3cret-value")


def test_the_explicit_file_backend_is_honoured_too(linux_host, monkeypatch):
    monkeypatch.setenv("AXIOM_FOREIGN_SECRETS_BACKEND", "file")
    _store_like_secrets_set(linux_host, "site-ingest", "v2")
    secret = resolve(SecretRef.parse("keychain://site-ingest"))
    assert getattr(secret, "value", secret) in (b"v2", "v2")


def test_outside_dev_with_no_backend_it_still_refuses_clearly(linux_host, monkeypatch):
    monkeypatch.setenv("AXIOM_MODE", "production")
    with pytest.raises(Exception) as err:
        resolve(SecretRef.parse("keychain://site-ingest"))
    assert "keychain" in str(err.value).lower() or "backend" in str(err.value).lower()
