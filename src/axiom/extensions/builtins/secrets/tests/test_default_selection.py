# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""ADR-003 — OpenBao is the default SecretStore backend, with a fail-closed
preflight and a dev-only degrade to ``env``.

The default is what a caller gets when no explicit scheme is named. It must be
OpenBao (self-hosted, enclave-appropriate), overridable by config, and it must
never silently fall back to plaintext ``env`` outside ``AXIOM_MODE=dev``.
"""

from __future__ import annotations

import pytest

from axiom.extensions.builtins import secrets


class _StubProvider:
    def __init__(self, available: bool) -> None:
        self._available = available

    def available(self) -> bool:
        return self._available


def test_default_scheme_is_openbao(monkeypatch):
    monkeypatch.delenv("AXIOM_SECRETS_DEFAULT", raising=False)
    assert secrets.default_scheme() == "openbao"


def test_default_scheme_honors_env_override(monkeypatch):
    monkeypatch.setenv("AXIOM_SECRETS_DEFAULT", "AWS")  # case-insensitive
    assert secrets.default_scheme() == "aws"


def test_default_provider_returns_default_when_available(monkeypatch):
    monkeypatch.setenv("AXIOM_SECRETS_DEFAULT", "openbao")
    prov = _StubProvider(available=True)
    monkeypatch.setattr(
        secrets.SecretStoreRegistry, "get", lambda scheme: (lambda cfg: prov)
    )
    assert secrets.default_provider() is prov


def test_default_provider_dev_degrades_to_env(monkeypatch):
    monkeypatch.setenv("AXIOM_SECRETS_DEFAULT", "openbao")
    monkeypatch.setattr(secrets, "_mode", lambda: "dev")
    unavailable = _StubProvider(available=False)
    env_prov = _StubProvider(available=True)
    monkeypatch.setattr(
        secrets.SecretStoreRegistry,
        "get",
        lambda scheme: (lambda cfg: env_prov)
        if scheme == "env"
        else (lambda cfg: unavailable),
    )
    assert secrets.default_provider() is env_prov


def test_default_provider_prod_fails_closed(monkeypatch):
    """Outside dev, an unreachable default raises — no plaintext fallback."""
    monkeypatch.setenv("AXIOM_SECRETS_DEFAULT", "openbao")
    monkeypatch.setattr(secrets, "_mode", lambda: "production")
    monkeypatch.setattr(
        secrets.SecretStoreRegistry,
        "get",
        lambda scheme: (lambda cfg: _StubProvider(available=False)),
    )
    with pytest.raises(secrets.SecretStoreUnavailable):
        secrets.default_provider()
