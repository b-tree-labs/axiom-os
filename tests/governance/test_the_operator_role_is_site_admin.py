# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""The role that reads, invokes and governs at a site is ``site-admin`` (ADR-261).

It was called ``operator`` (ADR-151, ADR-222). The word names a job in consumer
domains, so a person reading "operator" on a platform screen could take it for that
job. The rights do not change, and grants already stored as ``operator`` keep
working through an alias at every place a role is read; new grants are written as
``site-admin``. The narrow ``admin`` role keeps its id and is shown as "Access admin".

Rows:

- ROLE-A1  an ``operator`` grant authorizes exactly what ``site-admin`` does, on every
           surface that reads a role.
- ROLE-A2  the registry defines ``site-admin`` and not ``operator``; a roles file that
           still says ``[role.operator]`` narrows ``site-admin``.
- ROLE-A3  new grants are written as ``site-admin``, and naming ``operator`` says so in
           one line.
- ROLE-A4  a stored ``operator`` grant is not rewritten by being read.
- ROLE-A5  display names read "Site admin" and "Access admin".
- ROLE-A6  HTTP refusals that name a role name the site admin, never an operator.
- ROLE-A7  authz rules and the actor context treat the two names as one role.
- ROLE-A8  dev personas and help personas default to ``site-admin`` and accept ``operator``.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

from axiom.governance.roles import (
    LEGACY_ROLE_ALIASES,
    SITE_ADMIN,
    canonical_role,
    canonical_roles,
    display_name,
    legacy_note,
    role_label,
)

LEGACY = "operator"


# ------------------------------------------------------------------- ROLE-A1


def test_role_a1_an_operator_grant_resolves_to_exactly_the_site_admin_scopes():
    from axiom.extensions.builtins.webgate.role_bundles import default_bundle_registry

    reg = default_bundle_registry()
    assert reg.resolve([LEGACY]) == reg.resolve([SITE_ADMIN])
    assert reg.resolve([LEGACY]) == ("*:govern", "*:invoke", "*:read")
    assert reg.get(LEGACY) == reg.get(SITE_ADMIN)
    assert reg.sites_for([LEGACY]) == reg.sites_for([SITE_ADMIN])


@pytest.mark.parametrize("surface", [
    "features", "settings-edit", "settings-pages", "actors", "receipts",
])
def test_role_a1_every_role_set_treats_operator_as_site_admin(surface):
    def decide(roles):
        if surface == "features":
            from axiom.extensions.builtins.features.api import may_set

            return may_set(set(roles))
        if surface == "settings-edit":
            from axiom.extensions.builtins.settings.api import may_edit

            return may_edit(roles)
        if surface == "settings-pages":
            from axiom.extensions.builtins.settings.pages import sections_for

            return [s["id"] for s in sections_for(list(roles))["settings"]]
        if surface == "actors":
            from axiom.extensions.builtins.actors.visibility import viewer_class

            return viewer_class(roles)
        from axiom.extensions.builtins.receipts.relevance import viewer_from_roles

        return viewer_from_roles(roles).scopes

    assert decide([LEGACY]) == decide([SITE_ADMIN])
    assert decide([LEGACY]) != decide(["viewer"])


def test_role_a1_a_real_key_minted_for_an_operator_reaches_what_a_site_admin_reaches():
    """End to end: the gate mints a self-service key, the composed app's authz judges it."""
    from axiom.extensions.builtins.webgate import role_check

    old, new = role_check.check_in_process([LEGACY, SITE_ADMIN])
    assert old.ok and new.ok, (old.problems, new.problems)
    assert old.scopes == new.scopes
    assert [r.outcome for r in old.results] == [r.outcome for r in new.results]


def test_role_a1_the_role_check_judges_operator_as_site_admin():
    from axiom.extensions.builtins.webgate import role_check

    assert SITE_ADMIN in role_check.REACHES and LEGACY not in role_check.REACHES
    assert role_check.judge(LEGACY, []) == role_check.judge(SITE_ADMIN, []) == []


# ------------------------------------------------------------------- ROLE-A2


def test_role_a2_the_registry_defines_site_admin_and_not_operator():
    from axiom.extensions.builtins.webgate.role_bundles import PERSONA_ROLES, default_bundle_registry

    roles = default_bundle_registry().roles()
    assert SITE_ADMIN in roles and LEGACY not in roles
    assert PERSONA_ROLES == ("reader", "contributor", SITE_ADMIN)
    assert LEGACY_ROLE_ALIASES == {LEGACY: SITE_ADMIN}


def test_role_a2_a_roles_file_naming_operator_narrows_site_admin(tmp_path):
    from axiom.extensions.builtins.webgate.role_bundles import default_bundle_registry, load_overrides

    f = tmp_path / "roles.toml"
    f.write_text('[role.operator]\nscopes = ["*:read", "*:govern"]\n')
    out = load_overrides(f, default_bundle_registry())
    assert out.resolve([SITE_ADMIN]) == ("*:govern", "*:read")
    assert out.resolve([LEGACY]) == ("*:govern", "*:read")
    assert LEGACY not in out.roles()


def test_role_a2_nobody_registers_a_retired_name():
    from axiom.extensions.builtins.webgate.role_bundles import BundleRegistry, ScopeBundle

    with pytest.raises(ValueError, match="site-admin"):
        BundleRegistry().register(ScopeBundle(role=LEGACY, scopes=("*:read",)))


# ------------------------------------------------------------------- ROLE-A3


@pytest.fixture
def gate(tmp_path: Path):
    from axiom.extensions.builtins.webgate import skills as gate_skills
    from axiom.infra.skills import SkillContext, SkillRegistry

    reg = SkillRegistry()
    gate_skills.bind(reg)
    ctx = SkillContext(registry=reg, state_dir=tmp_path, logger=logging.getLogger("t"))
    return reg, ctx, str(tmp_path / "gate-users.json")


def _stored(path):
    from axiom.webauth import load_user_records

    return {r["email"]: list(r["roles"]) for r in load_user_records(path)}


def test_role_a3_adduser_writes_site_admin_when_told_operator(gate):
    reg, ctx, f = gate
    r = reg.invoke("gate.adduser", {"email": "a@x.example", "password": "Correct-Horse-9",
                                    "role": [LEGACY], "accounts_file": f}, ctx)
    assert r.ok, r.errors
    assert _stored(f)["a@x.example"] == [SITE_ADMIN]
    assert any("site-admin" in a and "operator" in a for a in r.actions_taken)


def test_role_a3_gate_role_set_operator_writes_site_admin_with_one_note(gate):
    reg, ctx, f = gate
    assert reg.invoke("gate.adduser", {"email": "a@x.example", "password": "Correct-Horse-9",
                                       "role": ["viewer"], "accounts_file": f}, ctx).ok
    r = reg.invoke("gate.role", {"email": "a@x.example", "set": [LEGACY], "accounts_file": f}, ctx)
    assert r.ok, r.errors
    assert _stored(f)["a@x.example"] == [SITE_ADMIN]
    notes = [a for a in r.actions_taken if "is now called" in a]
    assert len(notes) == 1 and "site-admin" in notes[0]


def test_role_a3_where_role_and_remove_find_both_names(gate, tmp_path):
    reg, ctx, f = gate
    Path(f).write_text(json.dumps([
        {"email": "old@x.example", "password_hash": "scrypt$x", "roles": [LEGACY]},
        {"email": "new@x.example", "password_hash": "scrypt$x", "roles": [SITE_ADMIN]},
        {"email": "v@x.example", "password_hash": "scrypt$x", "roles": ["viewer"]},
    ]))
    r = reg.invoke("gate.role", {"where_role": SITE_ADMIN, "remove": [LEGACY], "add": ["viewer"],
                                 "accounts_file": f}, ctx)
    assert r.ok, r.errors
    assert _stored(f) == {"old@x.example": ["viewer"], "new@x.example": ["viewer"],
                          "v@x.example": ["viewer"]}


def test_role_a3_an_invitation_for_operator_grants_site_admin(gate, tmp_path, monkeypatch):
    reg, ctx, _ = gate
    monkeypatch.setenv("AXIOM_GATE_API_KEYS_FILE", str(tmp_path / "keys.json"))
    r = reg.invoke("gate.invite", {"role": [LEGACY], "site": "example-loop", "principal": "@x:example-loop",
                                   "invitations_file": str(tmp_path / "invites.json")}, ctx)
    assert r.ok, r.errors
    assert r.value["roles"] == [SITE_ADMIN]


# ------------------------------------------------------------------- ROLE-A4


def test_role_a4_a_stored_operator_grant_is_read_not_rewritten(tmp_path):
    from axiom.extensions.builtins.webgate.role_bundles import default_bundle_registry
    from axiom.webauth import JsonFileUserStore

    f = tmp_path / "gate-users.json"
    f.write_text(json.dumps([{"email": "old@x.example", "password_hash": "scrypt$x",
                                        "roles": [LEGACY]}]))
    before = f.read_bytes()
    user = JsonFileUserStore(str(f)).get_by_email("old@x.example")
    assert default_bundle_registry().resolve(user.roles) == default_bundle_registry().resolve([SITE_ADMIN])
    assert f.read_bytes() == before


# ------------------------------------------------------------------- ROLE-A5


def test_role_a5_display_names():
    assert display_name(SITE_ADMIN) == "Site admin"
    assert display_name(LEGACY) == "Site admin"
    assert display_name("admin") == "Access admin"
    assert role_label(LEGACY) == "Site admin (site-admin)"
    assert canonical_role(LEGACY) == SITE_ADMIN and canonical_role("admin") == "admin"
    assert canonical_roles([LEGACY, SITE_ADMIN, "viewer"]) == (SITE_ADMIN, "viewer")
    assert "site-admin" in (legacy_note(LEGACY) or "") and legacy_note(SITE_ADMIN) is None


def test_role_a5_every_registered_role_has_a_display_name_and_none_says_operator():
    from axiom.extensions.builtins.webgate.role_bundles import default_bundle_registry

    for role in default_bundle_registry().roles():
        name = display_name(role)
        assert name and name != role or role in {"guest"} or name[0].isupper()
        assert "operator" not in name.lower()


def test_role_a5_the_gate_list_shows_display_names(gate):
    reg, ctx, f = gate
    Path(f).write_text(json.dumps([
        {"email": "old@x.example", "password_hash": "scrypt$x", "roles": [LEGACY, "admin"]}]))
    r = reg.invoke("gate.list", {"accounts_file": f}, ctx)
    assert r.ok, r.errors
    text = "\n".join(r.actions_taken) + json.dumps(r.value)
    assert "Site admin" in text and "Access admin" in text


# ------------------------------------------------------------------- ROLE-A6


def test_role_a6_http_refusals_name_the_site_admin():
    from fastapi import APIRouter, FastAPI
    from fastapi.testclient import TestClient

    from axiom.extensions.builtins.features import api as features_api
    from axiom.extensions.builtins.settings import api as settings_api

    app = FastAPI()
    for mod, sub in ((features_api, "/features"), (settings_api, "/settings")):
        router = APIRouter()
        mod.register_routes(router, subpath=sub)
        app.include_router(router)
    claims = {"sub": "v", "site": "example-loop", "roles": ["viewer"]}
    import unittest.mock as m

    with m.patch.object(features_api, "_claims", return_value=claims), \
            m.patch.object(settings_api, "_claims", return_value=claims):
        c = TestClient(app)
        details = [
            c.post("/features/x/kill", json={"reason": "r"}).json()["detail"],
            c.patch("/settings", json={"key": "k", "value": 1}).json()["detail"],
        ]
    for d in details:
        assert "site admin" in d and "operator" not in d


# ------------------------------------------------------------------- ROLE-A7


def test_role_a7_an_authz_rule_and_the_actor_treat_both_names_as_one():
    from axiom.extensions.builtins.authz.rules import Rule
    from axiom.governance.actor import ActorContext

    old = ActorContext(handle="@a:s", roles=(LEGACY,))
    new = ActorContext(handle="@b:s", roles=(SITE_ADMIN,))
    assert old.has_role(SITE_ADMIN) and new.has_role(LEGACY)
    from axiom.governance import IntentPattern, ResourcePattern

    for rule_roles in ({SITE_ADMIN}, {LEGACY}):
        rule = Rule(name="r", intent_pattern=IntentPattern("*"), actor_pattern="*",
                    resource_pattern=ResourcePattern("*"), roles=frozenset(rule_roles))
        assert rule.roles == frozenset({SITE_ADMIN})


# ------------------------------------------------------------------- ROLE-A8


def test_role_a8_dev_personas_default_to_site_admin_and_accept_operator(tmp_path):
    from axiom.extensions.builtins.dev import config

    assert config.DEFAULT_PERSONAS[SITE_ADMIN] == SITE_ADMIN
    assert LEGACY not in config.DEFAULT_PERSONAS
    assert config.canonical_persona(LEGACY) == SITE_ADMIN
    (tmp_path / "dev.toml").write_text('[personas]\noperator = "operator"\n')
    cfg = config.load(tmp_path)
    assert cfg.personas[SITE_ADMIN] == SITE_ADMIN and LEGACY not in cfg.personas


def test_role_a8_help_personas_read_operator_as_site_admin():
    from axiom.cli import help_engine

    assert SITE_ADMIN in help_engine.ROLES and LEGACY not in help_engine.ROLES
    assert help_engine.expand_intents([LEGACY]) == help_engine.expand_intents([SITE_ADMIN])
