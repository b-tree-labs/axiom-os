# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""The attest skills from the caller's side: a person at a prompt signs, and
an exported package verifies with a bare interpreter."""

from __future__ import annotations

import json
import logging
import subprocess
import sys

import pytest

from axiom.extensions.builtins.attest import service, signing
from axiom.extensions.builtins.attest.skills import SPECS, bind_default
from axiom.infra.principal import PrincipalContext
from axiom.infra.skills import SkillContext

SITE = "site-a"


@pytest.fixture
def bound_signer(node_signer):
    signing.set_provider(lambda: node_signer)
    yield node_signer
    signing.reset_provider()


@pytest.fixture
def roles(tmp_path):
    (tmp_path / "attest").mkdir()
    (tmp_path / "attest" / "roles.toml").write_text(
        '[[assignment]]\nprincipal = "@op1:site-a"\nsite = "site-a"\nroles = ["operator"]\n'
    )
    return tmp_path


def ctx(state_dir, answers=None, posture="sso", handle="@op1:site-a"):
    replies = list(answers or [])
    shown: list[str] = []

    def prompt(text):
        shown.append(text)
        return replies.pop(0)

    return SkillContext(
        registry=bind_default(),
        state_dir=state_dir,
        logger=logging.getLogger("test.attest"),
        user_prompt=prompt if answers is not None else None,
        principal=PrincipalContext(handle=handle, posture=posture, assured=True),
    ), shown


NEW = {
    "site": SITE,
    "logbook": "demo_log",
    "type": "ROUND_CHECK",
    "meaning": "performed",
    "title": "Round check",
    "fields": ["reading=ok", "walkdown=true"],
}


def _run(name, params, c):
    if isinstance(c, tuple):
        c = c[0]
    return c.registry.invoke(name, params, c)


def test_a_person_signs_at_the_prompt(attest_db, bound_signer, roles):
    c, shown = ctx(roles, answers=["sign"])
    r = _run("attest.new", NEW, c)
    assert r.ok, r.errors
    assert r.value["status"] == "signed" and r.value["seq"] == 1
    assert "Walkdown performed: yes" in shown[0]
    # The digest shown is the one the record carries as evidence.
    rec = service.record(r.value["attestation_id"])
    assert rec["evidence"]["presentation_digest"] in shown[0]


def test_no_one_at_the_prompt_signs_nothing(attest_db, bound_signer, roles):
    r = _run("attest.new", NEW, ctx(roles, answers=None))
    assert not r.ok and "person at the prompt" in r.errors[0]
    assert service.records(SITE, "demo_log") == []
    # Nothing was drafted either: refusing up front leaves no orphan behind.
    from sqlalchemy import text

    from axiom.extensions.builtins.attest import store

    with store.session_scope() as sess:
        assert sess.execute(text("SELECT count(*) FROM attest_drafts")).scalar() == 0


def test_hold_records_nothing(attest_db, bound_signer, roles):
    r = _run("attest.new", NEW, ctx(roles, answers=["hold"]))
    assert r.ok and r.value["status"] == "hold"
    assert service.records(SITE, "demo_log") == []


def test_no_assigned_role_refuses(attest_db, bound_signer, tmp_path):
    r = _run("attest.new", NEW, ctx(tmp_path, answers=["sign"]))
    assert not r.ok and "holds no role" in r.errors[0]


def test_open_posture_refuses(attest_db, bound_signer, roles):
    r = _run("attest.new", NEW, ctx(roles, answers=["sign"], posture="open"))
    assert not r.ok and "posture" in r.errors[0]


def test_sign_completes_a_software_draft(attest_db, bound_signer, roles):
    d = service.create_draft(
        site_id=SITE,
        logbook="demo_log",
        entry_type="ROUND_CHECK",
        meaning="performed",
        content={"title": "Round check", "fields": {"walkdown": True}},
        origin="agent:helper",
        for_principal="@op1:site-a",
    )
    r = _run("attest.sign", {"draft_id": d, "fields": ["reading=ok"]}, ctx(roles, answers=["sign"]))
    assert r.ok, r.errors
    assert service.record(r.value["attestation_id"])["origin"] == "agent:helper"


def test_show_and_verify(attest_db, bound_signer, roles):
    for _ in range(2):
        assert _run("attest.new", NEW, ctx(roles, answers=["sign"])).ok
    shown = _run("attest.show", {"site": SITE, "logbook": "demo_log"}, ctx(roles))
    assert [r["seq"] for r in shown.value["records"]] == [1, 2]
    one = _run(
        "attest.show", {"attestation_id": shown.value["records"][0]["attestation_id"]}, ctx(roles)
    )
    assert one.value["record"]["seq"] == 1
    v = _run("attest.verify", {"site": SITE, "logbook": "demo_log"}, ctx(roles))
    assert v.ok and v.value["checked"] == 2


def test_exported_package_verifies_with_a_bare_interpreter(
    attest_db, bound_signer, roles, tmp_path
):
    for _ in range(3):
        assert _run("attest.new", NEW, ctx(roles, answers=["sign"])).ok
    out = tmp_path / "package"
    r = _run("attest.export", {"site": SITE, "logbook": "demo_log", "out": str(out)}, ctx(roles))
    assert r.ok, r.errors
    assert sorted(p.name for p in out.iterdir()) == [
        "keys.json",
        "manifest.json",
        "records.jsonl",
        "verify.py",
    ]
    # -I: isolated mode, no site-packages and no Axiom on the path.
    run = subprocess.run(
        [sys.executable, "-I", "verify.py", "records.jsonl", "--keys", "keys.json"],
        cwd=out,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert run.returncode == 0, run.stdout + run.stderr
    assert json.loads((out / "manifest.json").read_text())["head_digest"] in run.stdout


def test_signing_skills_are_never_exposed_to_software():
    by_name = {s.name: s for s in SPECS}
    for name in ("attest.new", "attest.sign"):
        assert by_name[name].surfaces == ("cli",)
    for s in SPECS:
        if "mcp" in (s.surfaces or ()):
            assert s.side_effects is False, s.name


def test_scheduled_anchor_covers_every_site_with_a_chain(attest_db, bound_signer, roles):
    assert _run("attest.new", NEW, ctx(roles, answers=["sign"])).ok
    r = _run("attest.anchor", {}, ctx(roles))
    assert r.ok, r.errors
    assert [a["site_id"] for a in r.value["anchors"]] == [SITE]
    v = _run("attest.verify", {"site": SITE, "logbook": "demo_log"}, ctx(roles))
    assert v.ok and v.value["anchor"]["ok"] is True


def test_verify_reports_no_anchor_rather_than_a_pass(attest_db, bound_signer, roles):
    assert _run("attest.new", NEW, ctx(roles, answers=["sign"])).ok
    v = _run("attest.verify", {"site": SITE, "logbook": "demo_log"}, ctx(roles))
    assert v.ok and v.value["anchor"] is None


def test_anchor_refuses_when_the_node_cannot_sign(attest_db, roles):
    from axiom.vega.identity.node_key import NodeKeyUnavailable

    def broken():
        raise NodeKeyUnavailable("no node identity")

    signing.set_provider(broken, keys=dict)
    try:
        r = _run("attest.anchor", {"site": SITE}, ctx(roles))
    finally:
        signing.reset_provider()
    assert not r.ok and "no anchor written" in r.errors[0]


def test_device_enrolment_and_location_code_from_the_cli(attest_db, roles):
    from axiom.extensions.builtins.attest import presence

    presence.set_secret_provider(lambda site, loc: bytes(32) if loc == "control_room" else None)
    try:
        e = _run(
            "attest.device_enroll",
            {"site": SITE, "device_id": "console-a", "device_class": "kiosk",
             "location": "control_room", "mobility": "fixed"},
            ctx(roles),
        )  # fmt: skip
        assert e.ok, e.errors
        listed = _run("attest.device_list", {"site": SITE}, ctx(roles))
        assert [d["device_id"] for d in listed.value["devices"]] == ["console-a"]
        code = _run("attest.location_code", {"site": SITE, "location": "control_room"}, ctx(roles))
        assert code.ok and len(code.value["code"]) == 6
        none = _run("attest.location_code", {"site": SITE, "location": "lab"}, ctx(roles))
        assert not none.ok and "no secret" in none.errors[0]
        assert _run("attest.device_retire", {"device_id": "console-a"}, ctx(roles)).ok
        assert _run("attest.device_list", {"site": SITE}, ctx(roles)).value["devices"] == []
    finally:
        presence.reset_secret_provider()


def test_logbook_validate_reports_what_is_declared_but_not_yet_enforced(tmp_path):
    from axiom.extensions.builtins.attest.skills import logbook

    f = tmp_path / "b.toml"
    f.write_text(
        '[logbook]\nid = "b"\nversion = "1"\n\n[[type]]\nid = "T"\nmeanings = ["authored"]\n'
        'roles = ["r"]\ncosign = { roles = ["r"] }\n'
    )
    r = logbook.validate({"path": str(f)})
    assert r.ok and r.value["not_yet_enforced"] == ["T.cosign"]
    f.write_text(
        '[logbook]\nid = "b"\nversion = "1"\n\n[[type]]\nid = "T"\nmeanings = ["authored"]\nrolse = ["r"]\n'
    )
    r = logbook.validate({"path": str(f)})
    assert not r.ok and "rolse" in r.errors[0]


def test_roles_from_the_cli_are_what_signing_reads(attest_db, bound_signer, tmp_path):
    c = ctx(tmp_path)
    g = _run("attest.role_grant", {"site": SITE, "principal": "@op1:site-a", "role": "operator"}, c)
    assert g.ok, g.errors
    listed = _run("attest.role_list", {"site": SITE}, c)
    assert listed.value["assignments"] == [
        {"principal": "@op1:site-a", "site": SITE, "roles": ["operator"]}
    ]
    assert _run("attest.new", NEW, ctx(tmp_path, answers=["sign"])).ok
    assert _run(
        "attest.role_revoke", {"site": SITE, "principal": "@op1:site-a", "role": "operator"}, c
    ).ok
    r = _run("attest.new", NEW, ctx(tmp_path, answers=["sign"]))
    assert not r.ok and "holds no role" in r.errors[0]
