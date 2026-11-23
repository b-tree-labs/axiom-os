# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""The dev node's first account carries the person's name, not the local part of their address.

The account chip read "12017+benjaminbooth": a GitHub noreply address is the commit identity
many people use, and with no name on the account the chip fell back to it. The name comes from
the same place the address does (``git config user.name``), and an account made before this
gains it without its password changing.
"""

from __future__ import annotations

from axiom.extensions.builtins.dev import node
from axiom.webauth import load_user_records

from .conftest import FakeVault

ADDRESS = "12017+someone@users.noreply.github.com"


def test_a_new_account_is_named(tmp_path):
    plan = node.build_plan(lane="x", root=tmp_path, port=1, state_dir=tmp_path / "state")
    node.ensure_owner(plan, ADDRESS, FakeVault(), name="Pat Example")
    assert [r.get("name") for r in load_user_records(plan.accounts)] == ["Pat Example"]


def test_an_existing_unnamed_account_is_named_and_keeps_its_password(tmp_path):
    plan = node.build_plan(lane="x", root=tmp_path, port=1, state_dir=tmp_path / "state")
    vault = FakeVault()
    node.ensure_owner(plan, ADDRESS, vault)
    before = load_user_records(plan.accounts)[0]
    held = dict(vault.values)
    got = node.ensure_owner(plan, ADDRESS, vault, name="Pat Example")
    after = load_user_records(plan.accounts)[0]
    assert after["name"] == "Pat Example" and not got["created"]
    assert after["password_hash"] == before["password_hash"] and vault.values == held
