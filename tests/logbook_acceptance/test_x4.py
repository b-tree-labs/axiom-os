# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Logbook unit X4: the action ledger links to attestations, and GUARD owns the log skills.

Each test carries its acceptance id from the unit's ticket (LB-X4-A1 to A6). An action a person
authorised, once run, writes one ledger record whose ``authorised_by`` is the signed decision;
the decision's record links back. No domain words.
"""

from __future__ import annotations

import logging

import pytest

pytest.importorskip("fastapi")

from axiom.extensions.builtins.attest import service  # noqa: E402
from axiom.extensions.builtins.attest.service import AttestRefused  # noqa: E402
from axiom.infra.orchestrator.actions import Action, ActionCategory, ActionStatus  # noqa: E402
from axiom.infra.orchestrator.runner import ActionRunner  # noqa: E402
from axiom.infra.skills import SkillContext, SkillRegistry, SkillResult, SkillSpec  # noqa: E402

from . import test_x3  # noqa: E402
from .test_x3 import SITE, held, present_decision, sign_decision  # noqa: E402

#: The approvals gate fixture X3 defines: a durable gate, the approvals log and the seam installed.
gate = test_x3.gate

KEY = "test-ledger-key"


@pytest.fixture
def ledger_env(tmp_path, monkeypatch):
    """The action ledger on its JSONL backend in this test's state dir, chained."""
    from axiom.policy import action_ledger

    monkeypatch.setenv("AXIOM_ACTION_LEDGER_BACKEND", "jsonl")
    monkeypatch.setenv("AXIOM_AUDIT_HMAC_KEY", KEY)
    action_ledger._reset_backend_cache()
    yield tmp_path
    action_ledger._reset_backend_cache()


def _ledger(state_dir):
    from axiom.policy.action_ledger import ActionLedger

    return ActionLedger(state_dir=state_dir, hmac_key=KEY)


def _runner(gate_, state_dir, ok=True):
    reg = SkillRegistry()
    reg.register_skill(
        SkillSpec(
            name="demo.write_file",
            fn=lambda params, ctx: SkillResult(ok=ok, value={"wrote": params.get("path")}, errors=[] if ok else ["no"]),
            idempotent=True,
        )
    )
    ctx = SkillContext(registry=reg, state_dir=state_dir, logger=logging.getLogger("t.x4"), actor="@runner:site-a")
    return ActionRunner(gate_, reg, ctx)


def _approved_by_signature(gate_, signer):
    action = held(gate_, name="demo.write_file")
    rec = sign_decision(present_decision(action), signer).record
    return gate_.get(action.action_id), rec


def test_lb_x4_a1_a_run_writes_the_link_and_only_for_a_signed_decision(gate, node_signer, ledger_env):
    ledger = _ledger(ledger_env)
    ledger.record_action(agent="tidy", op_class="x.y", name="old", candidate="c", guards=["act"], outcome="proceeded")
    action, rec = _approved_by_signature(gate, node_signer)
    # A rule decision runs too, and writes nothing new.
    rule = gate.submit(Action(name="demo.write_file", category=ActionCategory.READ))
    assert rule.status == ActionStatus.APPROVED

    report = _runner(gate, ledger_env).run_approved()
    assert set(report.completed) == {action.action_id, rule.action_id}
    rows = ledger.query(limit=10)
    linked = [r for r in rows if r.get("authorised_by")]
    assert len(linked) == 1
    row = linked[0]
    assert row["authorised_by"] == rec["attestation_id"]
    assert (row["op_class"], row["name"], row["candidate"], row["outcome"]) == (
        "approval.run", "demo.write_file", action.action_id, "proceeded",
    )  # fmt: skip
    assert row["guards"] == ["approval"]
    # The old record keeps its exact shape: no authorised_by key appears on it.
    old = next(r for r in rows if r["name"] == "old")
    assert "authorised_by" not in old
    assert len(rows) == 2


def test_lb_x4_a2_old_chains_still_verify_and_an_edited_link_breaks(ledger_env, tmp_path):
    from sqlalchemy import create_engine, text

    from axiom.policy.action_ledger import ActionLedger

    for make in (
        lambda: _ledger(ledger_env),
        lambda: ActionLedger(
            state_dir=tmp_path, hmac_key=KEY,
            engine=create_engine(f"sqlite:///{tmp_path / 'ledger.db'}", future=True),
        ),  # fmt: skip
    ):
        ledger = make()
        for i in range(3):  # records written as before the change
            ledger.record_action(agent="a", op_class="o", name=f"n{i}", candidate="c", guards=[], outcome="proceeded")
        assert make().verify_chain() == (True, None)
        ledger.record_action(
            agent="a", op_class="approval.run", name="n", candidate="act", guards=["approval"],
            outcome="proceeded", authorised_by="att-1",
        )  # fmt: skip
        assert make().verify_chain() == (True, None)
    # Edit the link on the SQL row: the chain breaks there.
    eng = create_engine(f"sqlite:///{tmp_path / 'ledger.db'}", future=True)
    with eng.begin() as c:
        c.execute(text("UPDATE agent_actions SET authorised_by = 'att-2' WHERE authorised_by = 'att-1'"))
    ok, at = ActionLedger(state_dir=tmp_path, hmac_key=KEY, engine=eng).verify_chain()
    assert not ok and at == 3


def test_lb_x4_a3_the_links_read_both_ways(gate, node_signer, ledger_env, monkeypatch):
    action, rec = _approved_by_signature(gate, node_signer)
    _runner(gate, ledger_env).run_approved()
    from axiom.extensions.builtins.authz import skills as audit_skills

    reg = SkillRegistry()
    audit_skills.bind(reg)
    ctx = SkillContext(registry=reg, state_dir=ledger_env, logger=logging.getLogger("t"), user_prompt=None)
    r = reg.invoke("audit.actions", {"authorised_by": rec["attestation_id"]}, ctx)
    assert r.ok and [a["candidate"] for a in r.value["items"]] == [action.action_id]
    r = reg.invoke("audit.actions", {"authorised_by": "nothing-signed-this"}, ctx)
    assert r.ok and r.value["items"] == []

    links = service.links(rec["attestation_id"], state_dir=ledger_env)
    assert [a["candidate"] for a in links["actions"]] == [action.action_id]
    assert links["subjects"] == [{"kind": "action", "value": action.action_id}]

    from axiom.extensions.builtins.attest.skills import read

    shown = read.show({"attestation_id": rec["attestation_id"]}, SkillContext(
        registry=SkillRegistry(), state_dir=ledger_env, logger=logging.getLogger("t")))  # fmt: skip
    assert shown.ok and shown.value["links"]["actions"][0]["authorised_by"] == rec["attestation_id"]

    # An unreadable ledger is an empty list, never an error.
    from axiom.policy import action_ledger

    def broken(**_):
        raise RuntimeError("ledger unreadable")

    monkeypatch.setattr(action_ledger, "search_actions", broken)
    assert service.links(rec["attestation_id"], state_dir=ledger_env)["actions"] == []


def test_lb_x4_a4_the_link_is_consistent_and_both_chains_verify(gate, node_signer, ledger_env):
    action, rec = _approved_by_signature(gate, node_signer)
    _runner(gate, ledger_env).run_approved()
    row = next(r for r in _ledger(ledger_env).query(limit=5) if r.get("authorised_by"))
    assert rec["subjects"][0]["value"] == row["candidate"] == action.action_id
    assert _ledger(ledger_env).verify_chain() == (True, None)
    keys = {node_signer.key_id: node_signer.public_bytes}
    assert service.verify_logbook(SITE, "approvals", keys).ok

    # The runner never links to a record that does not decide this action.
    other = held(gate, name="demo.write_file")
    foreign = gate.get(other.action_id)
    foreign.approve(decided_by="@op2:site-a")
    foreign.attestation_id = rec["attestation_id"]  # another action's decision
    gate.record(foreign)
    ghost = held(gate, name="demo.write_file")
    g = gate.get(ghost.action_id)
    g.approve(decided_by="@op2:site-a")
    g.attestation_id = "no-such-record"
    gate.record(g)
    _runner(gate, ledger_env).run_approved()
    rows = _ledger(ledger_env).query(limit=20)
    assert [r["candidate"] for r in rows if r.get("authorised_by")] == [action.action_id]


def test_lb_x4_subjects_admit_artifacts_refs_and_runs_with_prov_roles(gate):
    ok = [
        {"kind": "artifact", "value": "sha256:" + "a" * 64, "role": "used"},
        {"kind": "ref", "value": "models:surrogate@3", "role": "generated"},
        {"kind": "run", "value": "run-0042", "role": "derived_from"},
        {"kind": "action", "value": "act1", "role": "attributed_to"},
    ]
    assert service._check_subjects(ok) == ok
    for bad in (
        [{"kind": "artifact", "value": "md5:abc"}],
        [{"kind": "artifact", "value": "sha256:short"}],
        [{"kind": "dataset", "value": "x"}],
        [{"kind": "run", "value": "r1", "role": "caused"}],
        [{"kind": "ref", "value": "no-version"}],
    ):
        with pytest.raises(AttestRefused):
            service._check_subjects(bad)


def test_lb_x4_a5_guard_owns_the_log_skills_on_every_surface():
    from axiom.agents.roster import load_roster, owner_index

    roster = load_roster(builtins_only=True)
    owners = [r.handle for r in roster if "attest" in r.namespaces]
    assert owners == ["guard"]
    index = owner_index(roster)
    assert index.owner_of("axiom_attest__show").display == "GUARD"
    assert index.owner_of("attest.verify").display == "GUARD"
    guard = next(r for r in roster if r.handle == "guard")
    assert "who signed this entry" in guard.asks


def test_lb_x4_a6_guard_has_no_signing_skill():
    from axiom.extensions.builtins.attest.skills import SPECS
    from axiom.extensions.builtins.connect.agent_router import discover_agents

    spec = discover_agents()["guard"]
    assert "attest" in spec.namespaces
    signing = {s.name for s in SPECS if s.surfaces == ("cli",)}
    assert {"attest.sign", "attest.seal", "attest.cosign", "attest.ack"} <= signing
    for s in SPECS:
        if s.name in signing:
            assert not set(s.surfaces) & {"mcp", "agent_tool"}, s.name
    from pathlib import Path

    import axiom.extensions.builtins.authz as authz

    persona = (Path(authz.__file__).parent / "agents" / "guard" / "persona.md").read_text(encoding="utf-8")
    assert "never signs, co-signs, acknowledges or seals" in persona
