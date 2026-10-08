# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0

"""IDENT-6: capability signature verification at the outbound (plaintext) site —
enforced at attested+, advisory at open. The canonical forgery (extend expiry)
is caught."""

from __future__ import annotations

import dataclasses
from datetime import timedelta

import pytest

from axiom.extensions.builtins.vault.capability_store import (
    VaultContext,
    issue_capability,
)
from axiom.extensions.builtins.vault.outbound import HttpRequest, HttpResponse, outbound_call
from axiom.governance import Classification, IntentPattern, ResourcePattern
from axiom.vega.identity.keypair import generate_keypair
from axiom.vega.identity.principal import Principal


def _ctx():
    return VaultContext(signer=generate_keypair())


def _cap(ctx):
    return issue_capability(
        ctx,
        subject=Principal(handle="@alice:test", public_bytes=b"\x00" * 32),
        intent_pattern=IntentPattern("*"),
        resource_pattern=ResourcePattern("*"),
        classification_ceiling=Classification.INTERNAL,
        secret_ref="s",
    )


def _req():
    return HttpRequest(method="GET", url="https://example.com/x", headers={})


def test_extend_expiry_forgery_rejected_at_attested(monkeypatch):
    monkeypatch.setenv("AXIOM_IDENTITY_POSTURE", "attested")
    ctx = _ctx()
    cap = _cap(ctx)
    forged = dataclasses.replace(cap, not_after=cap.not_after + timedelta(days=3650))
    with pytest.raises(ValueError, match="signature verification failed"):
        outbound_call(forged, _req(), ctx,
                      transport=lambda r: HttpResponse(status_code=200, headers={}, body=b"ok"),
                      credential_resolver=lambda _ref: "token")


def test_forgery_is_advisory_at_open(monkeypatch):
    monkeypatch.delenv("AXIOM_IDENTITY_POSTURE", raising=False)  # open (default)
    ctx = _ctx()
    cap = _cap(ctx)
    forged = dataclasses.replace(cap, not_after=cap.not_after + timedelta(days=3650))
    captured = []
    outbound_call(forged, _req(), ctx,
                  transport=lambda r: captured.append(r) or HttpResponse(status_code=200, headers={}, body=b"ok"),
                  credential_resolver=lambda _ref: "token")
    assert len(captured) == 1   # open posture: advisory — the call still proceeds


def test_legitimate_capability_passes_verification(monkeypatch):
    monkeypatch.setenv("AXIOM_IDENTITY_POSTURE", "attested")
    ctx = _ctx()
    cap = _cap(ctx)
    captured = []
    outbound_call(cap, _req(), ctx,
                  transport=lambda r: captured.append(r) or HttpResponse(status_code=200, headers={}, body=b"ok"),
                  credential_resolver=lambda _ref: "token")
    assert len(captured) == 1   # unforged, verifies, proceeds even at attested


# --- IDENT-9: require_mfa fresh second factor at release time ---

def _ok(r):
    return HttpResponse(status_code=200, headers={}, body=b"ok")


def test_require_mfa_proceeds_when_a_fresh_factor_confirms():
    ctx = VaultContext(signer=generate_keypair(), mfa_confirm=lambda: True)
    captured = []
    outbound_call(_cap(ctx), _req(), ctx, require_mfa=True,
                  transport=lambda r: captured.append(r) or _ok(r),
                  credential_resolver=lambda _ref: "token")
    assert len(captured) == 1


def test_require_mfa_denied_when_factor_declines():
    ctx = VaultContext(signer=generate_keypair(), mfa_confirm=lambda: False)
    with pytest.raises(ValueError, match="second factor"):
        outbound_call(_cap(ctx), _req(), ctx, require_mfa=True,
                      transport=_ok, credential_resolver=lambda _ref: "token")


def test_require_mfa_fails_closed_without_a_confirmer():
    ctx = VaultContext(signer=generate_keypair())   # no mfa_confirm wired
    with pytest.raises(ValueError, match="second factor"):
        outbound_call(_cap(ctx), _req(), ctx, require_mfa=True,
                      transport=_ok, credential_resolver=lambda _ref: "token")


# --- ENF-4: posture-floor enforcement (AEOS-ID-2) ---

def test_floored_credential_denied_below_posture():
    from axiom.infra.principal import PrincipalContext

    ctx = VaultContext(signer=generate_keypair(),
                       principal=PrincipalContext("@ben:local", "open"))   # below 'attested'
    with pytest.raises(ValueError, match="requires posture 'attested'"):
        outbound_call(_cap(ctx), _req(), ctx, min_posture="attested",
                      transport=_ok, credential_resolver=lambda _ref: "token")


def test_floored_credential_released_when_principal_meets_floor():
    from axiom.infra.principal import PrincipalContext

    ctx = VaultContext(signer=generate_keypair(),
                       principal=PrincipalContext("@ben:local", "attested", assured=True))
    captured = []
    outbound_call(_cap(ctx), _req(), ctx, min_posture="attested",
                  transport=lambda r: captured.append(r) or _ok(r),
                  credential_resolver=lambda _ref: "token")
    assert len(captured) == 1
