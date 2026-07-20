# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for the ``gate.*`` account-admin skills (ADR-056).

Exercises each skill through the SkillRegistry the way the CLI does, and asserts
the end-to-end property that matters: an account written by ``gate.adduser`` /
``gate.resetpw`` authenticates through the live :class:`JsonFileUserStore` the
gate reads from — the write and the gate converge on one file.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from axiom.extensions.builtins.webgate import skills as gate_skills
from axiom.infra.skills import SkillContext, SkillRegistry
from axiom.webauth import JsonFileUserStore, authenticate

PW = "Correct-Horse-9"


@pytest.fixture
def ctx(tmp_path: Path) -> SkillContext:
    reg = SkillRegistry()
    gate_skills.bind(reg)
    return SkillContext(
        registry=reg, state_dir=tmp_path, logger=logging.getLogger("test.gate")
    )


def _accounts(tmp_path: Path) -> str:
    return str(tmp_path / "gate-users.json")


# ---------- adduser -------------------------------------------------------


def test_adduser_creates_and_authenticates(ctx: SkillContext, tmp_path: Path):
    f = _accounts(tmp_path)
    r = ctx.registry.invoke("gate.adduser", {
        "email": "Op@UT.example", "password": PW,
        "role": ["operator", "staff"], "name": "Op", "accounts_file": f,
    }, ctx)
    assert r.ok, r.errors
    assert r.value["created"] is True
    assert r.value["roles"] == ["operator", "staff"]  # JSON-friendly list
    # the account authenticates through the store the gate uses
    store = JsonFileUserStore(f)
    user = authenticate(store, "op@ut.example", PW)
    assert user is not None and user.roles == ("operator", "staff")


def test_adduser_generates_password_when_omitted(ctx: SkillContext, tmp_path: Path):
    f = _accounts(tmp_path)
    r = ctx.registry.invoke("gate.adduser", {
        "email": "gen@ut.example", "role": ["student"], "accounts_file": f,
    }, ctx)
    assert r.ok, r.errors
    generated = r.value["password"]
    assert generated  # shown once
    assert authenticate(JsonFileUserStore(f), "gen@ut.example", generated) is not None


def test_adduser_rejects_weak_password(ctx: SkillContext, tmp_path: Path):
    r = ctx.registry.invoke("gate.adduser", {
        "email": "weak@ut.example", "password": "short",
        "accounts_file": _accounts(tmp_path),
    }, ctx)
    assert not r.ok
    assert "weak password" in r.errors[0]


def test_adduser_duplicate_needs_force(ctx: SkillContext, tmp_path: Path):
    f = _accounts(tmp_path)
    base = {"email": "dup@ut.example", "password": PW, "accounts_file": f}
    assert ctx.registry.invoke("gate.adduser", base, ctx).ok
    dup = ctx.registry.invoke("gate.adduser", base, ctx)
    assert not dup.ok and "already exists" in dup.errors[0]
    forced = ctx.registry.invoke("gate.adduser", {**base, "force": True,
                                                   "role": ["admin"]}, ctx)
    assert forced.ok and forced.value["created"] is False


def test_adduser_without_accounts_file_errors(ctx: SkillContext, monkeypatch):
    monkeypatch.delenv("AXIOM_GATE_USERS_FILE", raising=False)
    r = ctx.registry.invoke("gate.adduser", {"email": "x@y.z", "password": PW}, ctx)
    assert not r.ok
    assert "accounts file" in r.errors[0]


# ---------- resetpw -------------------------------------------------------


def test_resetpw_rotates_and_preserves_roles(ctx: SkillContext, tmp_path: Path):
    f = _accounts(tmp_path)
    ctx.registry.invoke("gate.adduser", {
        "email": "rot@ut.example", "password": PW,
        "role": ["operator"], "name": "Rot", "accounts_file": f,
    }, ctx)
    r = ctx.registry.invoke("gate.resetpw", {
        "email": "rot@ut.example", "password": "Brand-New-9", "accounts_file": f,
    }, ctx)
    assert r.ok, r.errors
    store = JsonFileUserStore(f)
    assert authenticate(store, "rot@ut.example", PW) is None            # old dead
    user = authenticate(store, "rot@ut.example", "Brand-New-9")
    assert user is not None and user.roles == ("operator",)             # roles kept
    assert user.name == "Rot"


def test_resetpw_generates_when_omitted(ctx: SkillContext, tmp_path: Path):
    f = _accounts(tmp_path)
    ctx.registry.invoke("gate.adduser", {
        "email": "g2@ut.example", "password": PW, "accounts_file": f}, ctx)
    r = ctx.registry.invoke("gate.resetpw", {"email": "g2@ut.example",
                                             "accounts_file": f}, ctx)
    assert r.ok
    assert authenticate(JsonFileUserStore(f), "g2@ut.example", r.value["password"])


def test_resetpw_unknown_account_errors(ctx: SkillContext, tmp_path: Path):
    f = _accounts(tmp_path)
    ctx.registry.invoke("gate.adduser", {
        "email": "known@ut.example", "password": PW, "accounts_file": f}, ctx)
    r = ctx.registry.invoke("gate.resetpw", {"email": "ghost@ut.example",
                                             "accounts_file": f}, ctx)
    assert not r.ok and "no such account" in r.errors[0]


def test_resetpw_missing_file_errors(ctx: SkillContext, tmp_path: Path):
    r = ctx.registry.invoke("gate.resetpw", {
        "email": "x@ut.example", "accounts_file": _accounts(tmp_path)}, ctx)
    assert not r.ok


# ---------- list ----------------------------------------------------------


def test_list_empty_when_uncreated(ctx: SkillContext, tmp_path: Path):
    r = ctx.registry.invoke("gate.list", {"accounts_file": _accounts(tmp_path)}, ctx)
    assert r.ok and r.value["items"] == []


def test_list_hides_password_hash(ctx: SkillContext, tmp_path: Path):
    f = _accounts(tmp_path)
    ctx.registry.invoke("gate.adduser", {
        "email": "seen@ut.example", "password": PW,
        "role": ["operator"], "accounts_file": f}, ctx)
    r = ctx.registry.invoke("gate.list", {"accounts_file": f}, ctx)
    assert r.ok
    item = r.value["items"][0]
    assert item["email"] == "seen@ut.example"
    assert item["roles"] == ["operator"]
    assert "password_hash" not in item and "password" not in item
