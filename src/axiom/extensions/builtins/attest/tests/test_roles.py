# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Giving people their signing roles (ADR-142, ADR-146 interim).

A person never states their own roles; an administrator assigns them on the
node, and every change is kept with who made it. Administration roles are
refused here because signing would discard them anyway (rule 8)."""

from __future__ import annotations

import json

import pytest

from axiom.extensions.builtins.attest import roles


def test_grant_then_lookup(tmp_path):
    roles.grant("@op1:site-a", "site-a", "reactor_operator", by="@admin:site-a", state_dir=tmp_path)
    assert roles.roles_for("@op1:site-a", "site-a", state_dir=tmp_path) == ("reactor_operator",)
    assert roles.roles_for("@op1:site-a", "site-b", state_dir=tmp_path) == ()


def test_grant_is_idempotent_and_roles_accumulate(tmp_path):
    for r in ("reactor_operator", "reactor_operator", "health_physics"):
        roles.grant("@op1:site-a", "site-a", r, by="@admin:site-a", state_dir=tmp_path)
    assert roles.roles_for("@op1:site-a", "site-a", state_dir=tmp_path) == (
        "reactor_operator",
        "health_physics",
    )


def test_revoke_removes_one_role(tmp_path):
    for r in ("reactor_operator", "health_physics"):
        roles.grant("@op1:site-a", "site-a", r, by="@admin:site-a", state_dir=tmp_path)
    roles.revoke(
        "@op1:site-a", "site-a", "reactor_operator", by="@admin:site-a", state_dir=tmp_path
    )
    assert roles.roles_for("@op1:site-a", "site-a", state_dir=tmp_path) == ("health_physics",)


def test_revoking_what_was_never_granted_is_an_error(tmp_path):
    with pytest.raises(ValueError, match="does not hold"):
        roles.revoke(
            "@op1:site-a", "site-a", "reactor_operator", by="@admin:site-a", state_dir=tmp_path
        )


def test_every_change_is_kept_with_who_made_it(tmp_path):
    roles.grant("@op1:site-a", "site-a", "reactor_operator", by="@admin:site-a", state_dir=tmp_path)
    roles.revoke(
        "@op1:site-a", "site-a", "reactor_operator", by="@admin2:site-a", state_dir=tmp_path
    )
    log = [
        json.loads(x) for x in roles.changes_file(tmp_path).read_text(encoding="utf-8").splitlines()
    ]
    assert [(e["action"], e["by"]) for e in log] == [
        ("grant", "@admin:site-a"),
        ("revoke", "@admin2:site-a"),
    ]
    assert all(e["at"] for e in log)


def test_listing_shows_every_assignment_at_a_site(tmp_path):
    roles.grant("@op1:site-a", "site-a", "reactor_operator", by="@a:x", state_dir=tmp_path)
    roles.grant("@op2:site-a", "site-a", "senior_reactor_operator", by="@a:x", state_dir=tmp_path)
    roles.grant("@op3:site-b", "site-b", "reactor_operator", by="@a:x", state_dir=tmp_path)
    assert roles.assignments("site-a", state_dir=tmp_path) == [
        {"principal": "@op1:site-a", "site": "site-a", "roles": ["reactor_operator"]},
        {"principal": "@op2:site-a", "site": "site-a", "roles": ["senior_reactor_operator"]},
    ]


@pytest.mark.parametrize(
    ("principal", "role", "message"),
    [
        ("op1", "reactor_operator", "handle"),
        ("@op1:site-a", "Reactor Operator", "role"),
        ("@op1:site-a", "node_admin", "administration"),
        ("@op1:site-a", "admin", "administration"),
    ],
)
def test_bad_grants_are_refused(tmp_path, principal, role, message):
    with pytest.raises(ValueError, match=message):
        roles.grant(principal, "site-a", role, by="@a:x", state_dir=tmp_path)


def test_the_file_survives_a_round_trip_through_the_reader(tmp_path):
    roles.grant("@op1:site-a", "site-a", "reactor_operator", by="@a:x", state_dir=tmp_path)
    import tomllib

    data = tomllib.loads(roles.roles_file(tmp_path).read_text(encoding="utf-8"))
    assert data["assignment"] == [
        {"principal": "@op1:site-a", "site": "site-a", "roles": ["reactor_operator"]}
    ]
