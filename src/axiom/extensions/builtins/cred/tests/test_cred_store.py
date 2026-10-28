# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0

"""CRED-1: the personal credential fabric — put/get/list/rm, with posture +
require_mfa gating on release."""

from __future__ import annotations

import pytest

from axiom.extensions.builtins.cred.store import (
    CredStore,
    InMemoryCredBackend,
    MfaRequired,
    PostureError,
)
from axiom.infra.principal import PrincipalContext

OPEN = PrincipalContext("@ben:local", "open")
ATTESTED = PrincipalContext("@ben:local", "attested", assured=True)


def _store():
    return CredStore(InMemoryCredBackend())


def test_put_get_roundtrip_on_open_floor():
    s = _store()
    s.put("private-host", "ssh-key-abc")
    assert s.get("private-host", principal=OPEN) == "ssh-key-abc"


def test_get_below_floor_is_blocked():
    s = _store()
    s.put("private-llm-key", "sk-real", min_posture="attested")
    with pytest.raises(PostureError, match="step up"):
        s.get("private-llm-key", principal=OPEN)
    assert s.get("private-llm-key", principal=ATTESTED) == "sk-real"   # meets the floor


def test_require_mfa_demands_a_fresh_factor():
    s = _store()
    s.put("box-token", "tok", require_mfa=True)
    with pytest.raises(MfaRequired):
        s.get("box-token", principal=OPEN)                          # no confirmer
    with pytest.raises(MfaRequired):
        s.get("box-token", principal=OPEN, mfa_confirm=lambda: False)
    assert s.get("box-token", principal=OPEN, mfa_confirm=lambda: True) == "tok"


def test_list_shows_names_and_floors_never_values():
    s = _store()
    s.put("a", "v1")
    s.put("b", "v2", min_posture="sso", require_mfa=True)
    listing = s.list()
    names = {e["name"] for e in listing}
    assert names == {"a", "b"}
    assert all("value" not in e for e in listing)                   # values never listed
    b = next(e for e in listing if e["name"] == "b")
    assert b["min_posture"] == "sso" and b["require_mfa"] is True


def test_rm_removes():
    s = _store()
    s.put("x", "v")
    assert s.rm("x") is True
    assert s.rm("x") is False
    with pytest.raises(KeyError):
        s.get("x", principal=OPEN)


def test_step_up_elevates_at_the_dereference_moment():
    s = _store()
    s.put("ec-key", "sk", min_posture="attested")
    # below floor + a step-up that elevates -> released (the ENF-3 moment)
    elevated = PrincipalContext("@ben:local", "attested", assured=True)
    assert s.get("ec-key", principal=OPEN, step_up=lambda _target: elevated) == "sk"
    # below floor + a step-up that can't elevate -> still denied
    with pytest.raises(PostureError):
        s.get("ec-key", principal=OPEN, step_up=lambda _target: OPEN)
