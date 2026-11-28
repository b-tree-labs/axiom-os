# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Acceptance tests for unit O1: one spelling for a signing role.

Ticket: docs/working/logbook-units/O1.md and O1-design-note.md in the product repo that holds
the plan. Rows A1 to A4 and A6 are Axiom's; A5 (the real logbook is unchanged) is the product's.
The fixture is `logbooks/role_log.toml` beside this file. Ids are invented (`shift_lead`).

The rule under test (design note, decision 3): hyphen and underscore are one character in a
signing role, the canonical id is the underscore form, and it is applied before any
administration check. Nothing is rewritten on disk.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

pytest.importorskip("fastapi")

from axiom.extensions.builtins.attest import registry, roles, site_settings  # noqa: E402
from axiom.extensions.builtins.attest.logbooks import LogbookError, parse_logbook  # noqa: E402
from axiom.extensions.builtins.attest.skills import SPECS  # noqa: E402
from axiom.extensions.builtins.attest.tests.test_api import OP1, as_, client  # noqa: E402, F401
from axiom.extensions.builtins.attest.tests.test_skills import _run, ctx  # noqa: E402

ROLE_LOG = Path(__file__).parent / "logbooks" / "role_log.toml"
SITE = "site-a"
WHO = "@op1:site-a"
ADMIN = "@admin:site-a"
NEW = {
    "site": SITE,
    "logbook": "role_log",
    "type": "ROUND_CHECK",
    "meaning": "performed",
    "title": "Round check",
    "fields": ["ok=true"],
}
ADMINISTRATION = ("admin", "node-admin", "node_admin", "developer", "vault-custodian")


def _canonical(role: str) -> str:
    return roles.canonical_signing_role(role)


def _write_roles(state_dir: Path, *held: str) -> Path:
    folder = state_dir / "attest"
    folder.mkdir(parents=True, exist_ok=True)
    body = ", ".join(json.dumps(r) for r in held)
    path = folder / "roles.toml"
    path.write_text(f'[[assignment]]\nprincipal = "{WHO}"\nsite = "{SITE}"\nroles = [{body}]\n')
    return path


@pytest.fixture
def role_log(attest_db):
    registry.register_file(ROLE_LOG)
    return registry.get("role_log")


@pytest.fixture
def no_site_pairs():
    site_settings.register(lambda site: {})
    yield
    site_settings.unregister()


@pytest.mark.acceptance("LB-O1-A1")
def test_o1_a1_either_spelling_is_one_stored_role(tmp_path):
    roles.grant(WHO, SITE, "shift-lead", by=ADMIN, state_dir=tmp_path)
    assert roles.roles_for(WHO, SITE, state_dir=tmp_path) == ("shift_lead",)
    roles.grant(WHO, SITE, "shift_lead", by=ADMIN, state_dir=tmp_path)
    rows = roles.assignments(SITE, state_dir=tmp_path)
    assert [r["roles"] for r in rows] == [["shift_lead"]], "one row, one role, not two"
    # and the reverse: revoking by the other spelling removes it
    roles.revoke(WHO, SITE, "shift-lead", by=ADMIN, state_dir=tmp_path)
    assert roles.roles_for(WHO, SITE, state_dir=tmp_path) == ()


@pytest.mark.acceptance("LB-O1-A2")
def test_o1_a2_a_role_written_with_a_hyphen_before_this_unit_still_signs(
    role_log, node_signer, bound_signer, tmp_path
):
    path = _write_roles(tmp_path, "shift-lead")
    before = path.read_bytes()
    c, _shown = ctx(tmp_path, answers=["sign"], handle=WHO)

    r = _run("attest.new", NEW, c)

    assert r.ok, r.errors
    assert r.value["status"] == "signed"
    assert path.read_bytes() == before, "signing must not rewrite the role file"
    assert not roles.changes_file(tmp_path).exists(), "no change history is made or altered"


@pytest.mark.acceptance("LB-O1-A3")
@pytest.mark.parametrize("held", ["operator", "admin", "owner", "node-admin", "node_admin", "developer"])
def test_o1_a3_no_governing_or_administration_role_becomes_a_signing_role(
    held, role_log, node_signer, bound_signer, tmp_path
):
    assert _canonical(held) != "shift_lead"
    assert _canonical(held) == held.replace("-", "_"), "spelling is the only thing normalising changes"
    _write_roles(tmp_path, held)
    c, _shown = ctx(tmp_path, answers=["sign"], handle=WHO)

    r = _run("attest.new", NEW, c)

    assert not r.ok or r.value.get("status") != "signed", f"{held!r} signed a shift_lead type"


@pytest.mark.acceptance("LB-O1-A4")
@pytest.mark.parametrize("spelled", ADMINISTRATION)
def test_o1_a4_an_administration_role_is_refused_in_either_spelling(spelled, tmp_path, no_site_pairs):
    with pytest.raises(ValueError, match="administration"):
        roles.grant(WHO, SITE, spelled, by=ADMIN, state_dir=tmp_path)


@pytest.mark.acceptance("LB-O1-A4")
def test_o1_a4_a_gate_pair_naming_an_administration_role_is_ignored_in_either_spelling():
    site_settings.register_authoritative(
        lambda site: {"attest": {"gate_roles": {
            "node-admin": "shift_lead",  # administration on the gate side
            "desk-staff": "node-admin",  # administration on the signing side
            "desk-lead": "shift-lead",  # a fine pair, spelled with a hyphen
        }}}
    )  # fmt: skip
    try:
        assert roles.gate_pairs(SITE) == {"desk-lead": "shift_lead"}
    finally:
        site_settings.unregister()


@pytest.mark.acceptance("LB-O1-A4")
def test_o1_a4_a_type_that_lists_only_administration_roles_still_refuses_to_load():
    for spelled in ("node-admin", "node_admin"):
        data = {
            "logbook": {"id": "bad_log", "version": "1", "display": "Bad", "signed": True,
                        "assurance": {"posture": "sso"}},
            "type": [{
                "id": "T", "meanings": ["performed"], "roles": [spelled],
                "fields": [{"id": "ok", "type": "attest_checkbox", "label": "ok", "required": True}],
            }],
        }  # fmt: skip
        with pytest.raises(LogbookError, match="administration"):
            parse_logbook(data, source="o1-a4")


@pytest.mark.acceptance("LB-O1-A6")
def test_o1_a6_my_roles_is_a_read_and_no_granting_or_signing_skill_gained_a_surface():
    by_name = {s.name: s for s in SPECS}
    mine = by_name["attest.my_roles"]
    assert {"cli", "mcp", "agent_tool"} <= set(mine.surfaces)
    for name in ("attest.role_grant", "attest.role_revoke", "attest.role_list"):
        assert not {"mcp", "agent_tool"} & set(by_name[name].surfaces), name
    for name in ("attest.new", "attest.sign", "attest.cosign", "attest.seal", "attest.ack"):
        assert not {"mcp", "agent_tool"} & set(by_name[name].surfaces), name


@pytest.mark.acceptance("LB-O1-A6")
def test_o1_a6_my_roles_returns_the_callers_roles_only(tmp_path):
    folder = tmp_path / "attest"
    folder.mkdir()
    (folder / "roles.toml").write_text(
        f'[[assignment]]\nprincipal = "{WHO}"\nsite = "{SITE}"\nroles = ["shift-lead"]\n\n'
        f'[[assignment]]\nprincipal = "@other:site-a"\nsite = "{SITE}"\nroles = ["auditor"]\n'
    )
    c, _ = ctx(tmp_path, handle=WHO)

    r = _run("attest.my_roles", {"site": SITE}, c)

    assert r.ok, r.errors
    assert list(r.value["roles"]) == ["shift_lead"]
    assert "auditor" not in json.dumps(r.value) and "@other" not in json.dumps(r.value)


@pytest.mark.acceptance("LB-O1-A6")
def test_o1_a6_the_roles_route_answers_the_signed_in_person_and_takes_no_write(client):  # noqa: F811
    assert client.get("/api/v1/attest/roles/me").status_code == 401
    r = as_(client, OP1).get("/api/v1/attest/roles/me", params={"site": "site-a"})
    assert r.status_code == 200, r.text
    assert r.json()["principal"] == WHO and r.json()["roles"] == ["operator"]
    assert as_(client, OP1).get("/api/v1/attest/roles/me", params={"site": "elsewhere"}).status_code == 403
    assert client.post("/api/v1/attest/roles/me", json={"roles": ["shift_lead"]}).status_code in (404, 405)
