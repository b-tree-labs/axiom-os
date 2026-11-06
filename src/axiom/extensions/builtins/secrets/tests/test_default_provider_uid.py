# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0

"""The default providers must keep one identity across restarts (ADR-012).

``uid`` is the stable primary key a provider is known by; ``config_hash``
detects config drift and ``instance_id`` is deliberately per-load. A default
provider that mints a fresh uid every process breaks the first of those, so
audit and rotation records cannot be correlated across a restart.
"""

from __future__ import annotations

import uuid

from axiom.extensions.builtins.secrets import _default_config_for_scheme

SCHEMES = ("aws", "azure", "env", "file", "gcp", "keychain", "kubernetes", "openbao")


def test_every_default_config_carries_a_uid():
    for scheme in SCHEMES:
        cfg = _default_config_for_scheme(scheme)
        assert cfg.get("uid"), f"{scheme}: default config has no uid"
        uuid.UUID(cfg["uid"])  # well-formed


def test_uid_is_stable_across_calls():
    for scheme in SCHEMES:
        assert _default_config_for_scheme(scheme)["uid"] == _default_config_for_scheme(scheme)["uid"]


def test_uid_is_distinct_per_scheme():
    uids = {s: _default_config_for_scheme(s)["uid"] for s in SCHEMES}
    assert len(set(uids.values())) == len(SCHEMES), uids


def test_uid_does_not_depend_on_secret_material(monkeypatch):
    """A token is config, but it must not move the provider's identity: the
    same provider with a rotated token is still the same provider."""
    monkeypatch.setenv("AXIOM_OPENBAO_TOKEN", "token-one")
    before = _default_config_for_scheme("openbao")["uid"]
    monkeypatch.setenv("AXIOM_OPENBAO_TOKEN", "token-two")
    assert _default_config_for_scheme("openbao")["uid"] == before


def test_constructing_a_default_provider_emits_no_uid_warning(caplog):
    from axiom.extensions.builtins.secrets import SecretStoreRegistry
    caplog.set_level("WARNING")
    provider_cls = SecretStoreRegistry.get("keychain")
    provider_cls(_default_config_for_scheme("keychain"))
    assert not [r for r in caplog.records if "has no 'uid' in config" in r.getMessage()]
