# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tests for the dev-only env SecretStoreProvider."""

from __future__ import annotations

import pytest

from axiom.extensions.builtins.secrets import (
    EnvSecretStoreProvider,
    SecretRef,
)


@pytest.fixture(autouse=True)
def _reset_mode(monkeypatch):
    monkeypatch.setenv("AXIOM_MODE", "dev")
    # Reset the warning suppression so tests are independent.
    from axiom.extensions.builtins.secrets.providers import env as env_mod
    env_mod._NON_DEV_WARNED.clear()
    yield


def test_get_returns_env_value(monkeypatch):
    monkeypatch.setenv("MY_SECRET", "hunter2")
    p = EnvSecretStoreProvider({"name": "dev"})
    store = p.open()
    s = store.get(SecretRef.parse("env://MY_SECRET"))
    assert s.value == b"hunter2"
    assert s.metadata["backend"] == "env"


def test_missing_env_raises_keyerror():
    p = EnvSecretStoreProvider({"name": "dev"})
    store = p.open()
    with pytest.raises(KeyError, match="not set"):
        store.get(SecretRef.parse("env://NO_SUCH_VAR_XYZ"))


def test_put_is_refused():
    p = EnvSecretStoreProvider({"name": "dev"})
    store = p.open()
    with pytest.raises(PermissionError, match="read-only"):
        store.put(SecretRef.parse("env://X"), b"v")


def test_list_paths_returns_matching_env_keys(monkeypatch):
    monkeypatch.setenv("AUDITED_FOO", "1")
    monkeypatch.setenv("AUDITED_BAR", "2")
    monkeypatch.setenv("UNRELATED", "3")
    p = EnvSecretStoreProvider({"name": "dev"})
    store = p.open()
    keys = store.list_paths("AUDITED_")
    assert "AUDITED_FOO" in keys
    assert "AUDITED_BAR" in keys
    assert "UNRELATED" not in keys


def test_prefix_config_scopes_keys(monkeypatch):
    monkeypatch.setenv("MYAPP_DB_PASSWORD", "p")
    p = EnvSecretStoreProvider({"name": "dev", "prefix": "MYAPP_"})
    store = p.open()
    # SecretRef.path="DB_PASSWORD" → looked up as MYAPP_DB_PASSWORD
    s = store.get(SecretRef.parse("env://DB_PASSWORD"))
    assert s.as_str() == "p"


def test_capabilities_advertise_dev_constraints():
    p = EnvSecretStoreProvider({"name": "dev"})
    caps = p.capabilities
    assert caps.read is True
    assert caps.write is False
    assert caps.encryption_at_rest is False
    assert caps.rotation is False
    assert caps.dynamic_credentials is False


def test_warns_when_used_outside_dev_mode(monkeypatch, caplog):
    monkeypatch.setenv("AXIOM_MODE", "production")
    with caplog.at_level("WARNING"):
        EnvSecretStoreProvider({"name": "prod-mistake"})
    assert any("intended for dev use only" in r.getMessage()
               for r in caplog.records)


def test_does_not_warn_in_dev_mode(monkeypatch, caplog):
    monkeypatch.setenv("AXIOM_MODE", "dev")
    with caplog.at_level("WARNING"):
        EnvSecretStoreProvider({"name": "dev-fine"})
    assert not any("dev use only" in r.getMessage() for r in caplog.records)


def test_warning_only_once_per_provider_uid(monkeypatch, caplog):
    monkeypatch.setenv("AXIOM_MODE", "staging")
    config = {"name": "stage", "uid": "stable-uid-1"}
    with caplog.at_level("WARNING"):
        EnvSecretStoreProvider(config)
        EnvSecretStoreProvider(config)  # same uid → no second warning
    warnings = [r for r in caplog.records
                if "dev use only" in r.getMessage()]
    assert len(warnings) == 1
