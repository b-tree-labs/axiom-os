# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""An administrator changes who may do what with one command, not a JSON edit.

On 2026-10-06 a site moved six sign-in accounts from ``viewer`` to a site
role, and the only way to do it was to hand-edit the accounts file on the node.
``gate.role`` sets, adds or removes roles on one account or on every account
holding a role, refuses a role the node does not define (a key minted from it
would authorise nothing), and never touches passwords.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from axiom.extensions.builtins.webgate import skills as gate_skills
from axiom.infra.skills import SkillContext, SkillRegistry
from axiom.webauth import JsonFileUserStore, authenticate, load_user_records

PW = "Correct-Horse-9"


@pytest.fixture
def ctx(tmp_path: Path) -> SkillContext:
    reg = SkillRegistry()
    gate_skills.bind(reg)
    return SkillContext(registry=reg, state_dir=tmp_path, logger=logging.getLogger("t"))


@pytest.fixture
def accounts(ctx, tmp_path) -> str:
    f = str(tmp_path / "gate-users.json")
    for email, roles in [("a@x.example", ["viewer"]), ("b@x.example", ["viewer"]),
                         ("root@x.example", ["admin", "operator"])]:
        r = ctx.registry.invoke("gate.adduser", {"email": email, "password": PW,
                                                 "role": roles, "accounts_file": f}, ctx)
        assert r.ok, r.errors
    return f


def _roles(f):
    return {r["email"]: list(r["roles"]) for r in load_user_records(f)}


def test_set_replaces_one_accounts_roles_and_keeps_its_password(ctx, accounts):
    r = ctx.registry.invoke("gate.role", {"email": "a@x.example", "set": ["operator"],
                                          "accounts_file": accounts}, ctx)
    assert r.ok, r.errors
    assert _roles(accounts)["a@x.example"] == ["operator"]
    assert _roles(accounts)["b@x.example"] == ["viewer"]
    assert authenticate(JsonFileUserStore(accounts), "a@x.example", PW) is not None


def test_every_holder_of_a_role_moves_at_once(ctx, accounts):
    r = ctx.registry.invoke("gate.role", {"where_role": "viewer", "add": ["operator"],
                                          "remove": ["viewer"], "accounts_file": accounts}, ctx)
    assert r.ok, r.errors
    got = _roles(accounts)
    assert got["a@x.example"] == ["operator"] and got["b@x.example"] == ["operator"]
    assert got["root@x.example"] == ["admin", "operator"]
    assert r.value["changed"] == 2


def test_a_role_the_node_does_not_define_is_refused(ctx, accounts):
    r = ctx.registry.invoke("gate.role", {"email": "a@x.example", "set": ["wizard"],
                                          "accounts_file": accounts}, ctx)
    assert not r.ok and "wizard" in r.errors[0]
    assert _roles(accounts)["a@x.example"] == ["viewer"]


def test_an_unknown_account_is_refused_not_created(ctx, accounts):
    r = ctx.registry.invoke("gate.role", {"email": "nobody@x.example", "set": ["viewer"],
                                          "accounts_file": accounts}, ctx)
    assert not r.ok
    assert "nobody@x.example" not in _roles(accounts)


def test_one_selector_and_one_change_are_required(ctx, accounts):
    assert not ctx.registry.invoke("gate.role", {"set": ["viewer"], "accounts_file": accounts}, ctx).ok
    assert not ctx.registry.invoke("gate.role", {"email": "a@x.example", "accounts_file": accounts}, ctx).ok
