# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""The invitation flow — a thin wrap over the platform gate invitation primitive.

``program invite`` mints a single-use, expiring, scrypt-hashed-at-rest code via
:mod:`axiom.webauth.invitations`; ``program redeem`` spends it (the code is the
authentication) and records the membership. The tests prove the reuse (the code
carries the primitive's prefix, the on-disk record is the primitive's shape),
the single-use lifecycle, the deputy gate on issue but not on redeem, and the
missing-account onboarding path.
"""

from __future__ import annotations

import copy
import json
import logging
from pathlib import Path
from typing import Any

import pytest

from axiom.extensions.builtins.program.model import load_program
from axiom.extensions.builtins.program.skills import _changelog as cl
from axiom.extensions.builtins.program.skills import invitation
from axiom.infra.principal import PrincipalContext
from axiom.infra.skills import SkillContext, SkillRegistry

DEPUTY = "@casey:example-org"

BASE: dict[str, Any] = {
    "schema": "axiom.program/0.1",
    "program": {"id": "example-program", "name": "Example", "deputy": DEPUTY},
    "lanes": [{"id": "alpha", "name": "Alpha"}],
    "people": [{"principal": DEPUTY, "name": "Casey", "lanes": ["alpha"]}],
    "schedule": [],
}


def _seed(tmp_path: Path, program: dict[str, Any]) -> Path:
    state = tmp_path / "state"
    (state / "program").mkdir(parents=True)
    (state / "program" / "data.json").write_text(json.dumps(program, indent=1), encoding="utf-8")
    return state


def _ctx(state: Path, *, principal: str = DEPUTY, assured: bool = False) -> SkillContext:
    return SkillContext(
        registry=SkillRegistry(),
        state_dir=state,
        logger=logging.getLogger("test.program.invite"),
        user_prompt=None,
        surface="cli",
        principal=PrincipalContext(
            handle=principal, posture=("attested" if assured else "open"), assured=assured
        ),
    )


def _reload(state: Path):
    return load_program(state / "program" / "data.json", require_listed_owners=False)


def _kinds(state: Path) -> list[str]:
    return [e["kind"] for e in cl.read_changelog(state / "program" / "changelog.jsonl")]


@pytest.fixture
def state(tmp_path: Path) -> Path:
    return _seed(tmp_path, copy.deepcopy(BASE))


def _invite(state, **params):
    params.setdefault("principal", "@newbie:example-org")
    params.setdefault("lane", "alpha")
    return invitation.invite(params, _ctx(state))


class TestInvite:
    def test_invite_mints_a_gate_invitation(self, state):
        r = _invite(state, role="eng")
        assert r.ok, r.errors
        # the reuse is visible: the primitive's own prefix on the code.
        assert r.value["code"].startswith("axi_inv_")
        # and the on-disk record is the primitive's shape (scrypt hash at rest).
        rows = json.loads((state / "program" / "invitations.json").read_text())
        assert rows[0]["code_hash"] and "code" not in rows[0]
        assert rows[0]["program"] == "example-program" and rows[0]["lane"] == "alpha"
        assert "invited" in _kinds(state)

    def test_invite_unknown_lane_is_refused(self, state):
        r = _invite(state, lane="ghost")
        assert not r.ok and r.value["refused"] == "bad_request"

    def test_invite_is_deputy_gated(self, state, monkeypatch):
        monkeypatch.setenv("AXIOM_IDENTITY_POSTURE", "sso")
        r = invitation.invite(
            {"principal": "@newbie:example-org", "lane": "alpha"},
            _ctx(state, principal="@stranger:example-org", assured=True),
        )
        assert not r.ok and r.value["refused"] == "forbidden"


class TestRedeem:
    def test_redeem_records_the_membership(self, state):
        code = _invite(state, role="eng", account=["forge=newb1"]).value["code"]
        r = invitation.redeem({"code": code}, _ctx(state, principal="@newbie:example-org"))
        assert r.ok, r.errors
        person = _reload(state).person("@newbie:example-org")
        assert person is not None and person["lanes"] == ["alpha"] and person["role"] == "eng"
        assert person["accounts"] == {"forge": "newb1"}
        kinds = _kinds(state)
        assert "redeemed" in kinds and "person_added" in kinds

    def test_redeem_is_single_use(self, state):
        code = _invite(state).value["code"]
        assert invitation.redeem({"code": code}, _ctx(state)).ok
        again = invitation.redeem({"code": code}, _ctx(state))
        assert not again.ok and again.value["refused"] == "forbidden"

    def test_a_garbage_code_is_refused(self, state):
        r = invitation.redeem({"code": "not-a-real-code"}, _ctx(state))
        assert not r.ok and r.value["refused"] == "forbidden"

    def test_redeem_is_not_deputy_gated(self, state, monkeypatch):
        # The code is the authentication, so even on a strict node a non-deputy
        # redeemer succeeds — the invitation was deputy-authorized at issue.
        code = _invite(state, principal="@guest:example-org").value["code"]
        monkeypatch.setenv("AXIOM_IDENTITY_POSTURE", "sso")
        r = invitation.redeem({"code": code}, _ctx(state, principal="@guest:example-org", assured=True))
        assert r.ok, r.errors
        assert _reload(state).person("@guest:example-org") is not None

    def test_missing_account_is_onboarded_not_blocked(self, state):
        # An invitee who declares no accounts becomes a member with none.
        code = _invite(state, principal="@partner:example-org").value["code"]
        r = invitation.redeem({"code": code}, _ctx(state))
        assert r.ok, r.errors
        person = _reload(state).person("@partner:example-org")
        assert person is not None
        assert "accounts" not in person or person["accounts"] == {}
