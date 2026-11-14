# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Logbook unit X3: the approval gate signs in an ``approvals`` log, and ``@gate:auto`` goes.

Each test carries its acceptance id from the unit's ticket (LB-X3-A1 to A8). The decision is a signed
``ACTION_DECISION`` record; with the flag ``approvals_signed`` on, the gate moves a held action
only on that record. No domain words.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

pytest.importorskip("fastapi")

from axiom.extensions.builtins.attest import approvals, grants, registry, service, signing  # noqa: E402
from axiom.extensions.builtins.attest import roles as role_store  # noqa: E402
from axiom.extensions.builtins.attest.grants import Authentication, Device, GrantRefused  # noqa: E402
from axiom.extensions.builtins.attest.service import AttestRefused, Signatory  # noqa: E402
from axiom.infra.orchestrator import approval_tokens, decisions  # noqa: E402
from axiom.infra.orchestrator import skills as approval_skills  # noqa: E402
from axiom.infra.orchestrator.actions import (  # noqa: E402
    Action,
    ActionCategory,
    ActionStatus,
    create_action,
    is_automatic,
)
from axiom.infra.orchestrator.approval import ApprovalGate, DecisionRefused  # noqa: E402
from axiom.infra.orchestrator.approval_store import FileActionStore  # noqa: E402
from axiom.infra.principal import PrincipalContext  # noqa: E402
from axiom.infra.skills import SkillContext, SkillRegistry  # noqa: E402

SITE = "site-a"
APPROVER = "@op2:site-a"
OTHER = "@op1:site-a"
DESK = Device(device_id="desk-1", device_class="personal")
SRC = Path(__file__).resolve().parents[2] / "src"
PARAMS = {"path": "notes/a.txt", "api_token": "s3cret-value", "mode": "append"}


@pytest.fixture
def gate(attest_db, node_signer, tmp_path, monkeypatch):
    """The durable gate the approval skills use, the approvals log, and the seam installed."""
    monkeypatch.setenv("AXIOM_SITE", SITE)
    monkeypatch.setenv("AXIOM_FLAG_APPROVALS_SIGNED", "on")
    registry.register_file(approvals.LOGBOOK_FILE)
    signing.set_provider(lambda: node_signer)
    g = ApprovalGate(FileActionStore(tmp_path / "orchestrator" / "approvals.json"))
    approvals.install(gate_factory=lambda: g)
    role_store.grant(APPROVER, SITE, "approver", by="@admin:site-a", state_dir=tmp_path)
    yield g
    approvals.uninstall()
    signing.reset_provider()


def approver(handle=APPROVER, roles=("approver",)) -> Signatory:
    return Signatory(
        principal=PrincipalContext(handle=handle, posture="sso", assured=True),
        roles=tuple(roles),
        display="Approver",
    )


def held(gate: ApprovalGate, params=None, name="write_file") -> Action:
    action = create_action(name, dict(PARAMS if params is None else params))
    action.reason = "a write needs a person"
    return gate.submit(action)


def _keys(signer):
    return {signer.key_id: signer.public_bytes}


def present_decision(action, *, approving=True, who=None):
    who = who or approver()
    d = service.create_decision_draft(
        site_id=SITE, action=action, approving=approving, reason="",
        origin="human", for_principal=who.principal.handle,
    )  # fmt: skip
    p = service.present(d, modality="screen")
    service.respond(p.presentation_id, principal=who.principal, answer="sign", via="screen")
    return p


def sign_decision(p, signer, *, who=None, minutes_ago=1):
    who = who or approver()
    auth = Authentication(
        principal=who.principal, auth_time=datetime.now(UTC) - timedelta(minutes=minutes_ago)
    )
    token = grants.mint(p.presentation_id, auth=auth, device=DESK, signer=signer)
    return service.sign(
        p.presentation_id, signatory=who, signer=signer, grant=token, grant_keys=_keys(signer)
    )


def skill_ctx(state_dir, *, handle=APPROVER, answers=None, surface="cli"):
    replies = list(answers or [])
    return SkillContext(
        registry=SkillRegistry(), state_dir=state_dir, logger=logging.getLogger("test.x3"),
        principal=PrincipalContext(handle=handle, posture="sso", assured=True),
        actor=handle, surface=surface,
        user_prompt=(lambda text: replies.pop(0)) if answers is not None else None,
    )  # fmt: skip


def test_lb_x3_a1_approving_signs_a_decision_and_the_gate_moves_only_on_it(gate, node_signer):
    action = held(gate)
    p = present_decision(action, approving=True)
    # Presented and answered, not yet signed: the action is still waiting.
    assert gate.get(action.action_id).status == ActionStatus.PENDING
    rec = sign_decision(p, node_signer).record
    assert (rec["logbook"], rec["kind"], rec["entry_type"], rec["meaning"]) == (
        "approvals", "statement", "ACTION_DECISION", "approved",
    )  # fmt: skip
    assert rec["subjects"] == [{"kind": "action", "value": action.action_id}]
    after = gate.get(action.action_id)
    assert after.status == ActionStatus.APPROVED
    assert after.decided_by == APPROVER
    assert after.attestation_id == rec["attestation_id"]


def test_lb_x3_a2_rejecting_signs_a_rejection_and_nothing_moves_without_a_record(
    gate, node_signer, tmp_path
):
    action = held(gate)
    rec = sign_decision(present_decision(action, approving=False), node_signer).record
    assert rec["meaning"] == "rejected"
    after = gate.get(action.action_id)
    assert after.status == ActionStatus.REJECTED and after.attestation_id == rec["attestation_id"]

    # With the flag on, the decision verb never moves an action by itself: a hold decides
    # nothing, and so does a call with nobody at the prompt.
    other = held(gate)
    r = approval_skills.approve({"action_id": other.action_id}, skill_ctx(tmp_path, answers=["hold"]))
    assert gate.get(other.action_id).status == ActionStatus.PENDING, r
    r = approval_skills.approve({"action_id": other.action_id}, skill_ctx(tmp_path))
    assert not r.ok and gate.get(other.action_id).status == ActionStatus.PENDING


def test_lb_x3_a3_a_read_is_a_rule_decision_and_gate_auto_is_gone(gate):
    read = gate.submit(Action(name="query_docs", category=ActionCategory.READ))
    assert read.status == ActionStatus.APPROVED
    assert read.decided_by == "rule:read_only" and read.attestation_id is None
    assert is_automatic(read.decided_by)

    # The literal is produced nowhere: only the legacy reader and the list of refused
    # machine resolvers may name it.
    allowed = {SRC / "axiom/infra/orchestrator/actions.py", SRC / "axiom/extensions/builtins/authz/graduation.py"}
    found = [
        f"{p.relative_to(SRC)}:{n}"
        for p in SRC.rglob("*.py")
        if "/tests/" not in str(p)
        for n, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1)
        if "@gate:auto" in line and p not in allowed
    ]
    assert found == []

    # A record stored before X3 still loads and reads as automatic; nothing rewrites it.
    old = Action.from_dict(
        {"action_id": "old1", "name": "query_docs", "category": "read", "status": "approved",
         "decided_by": "@gate:auto"}  # fmt: skip
    )
    assert old.decided_by == "@gate:auto" and is_automatic(old.decided_by)
    assert not is_automatic(APPROVER)

    # Deciding without naming who is no longer possible.
    with pytest.raises(TypeError):
        Action(name="write_file").approve()  # type: ignore[call-arg]
    with pytest.raises(TypeError):
        gate.approve(held(gate).action_id)  # type: ignore[call-arg]


def test_lb_x3_a4_decide_attested_is_idempotent_and_checks_the_record(gate, node_signer):
    action = held(gate)
    rec = sign_decision(present_decision(action), node_signer).record
    settled = gate.get(action.action_id)
    # The same record again changes nothing.
    again = gate.decide_attested(action.action_id, attestation=rec)
    assert again.to_dict() == settled.to_dict()
    # A different record on a settled action is refused.
    with pytest.raises(DecisionRefused, match="already"):
        gate.decide_attested(action.action_id, attestation={**rec, "attestation_id": "another"})

    pending = held(gate)
    # A record whose subject is another action is refused.
    with pytest.raises(DecisionRefused, match="subject"):
        gate.decide_attested(pending.action_id, attestation=rec)
    # A record signed by anything but a person is refused.
    machine = {
        **rec,
        "attestation_id": "m1",
        "subjects": [{"kind": "action", "value": pending.action_id}],
        "signer": {**rec["signer"], "kind": "agent"},
    }
    with pytest.raises(DecisionRefused, match="person"):
        gate.decide_attested(pending.action_id, attestation=machine)
    rule = {**machine, "signer": {**rec["signer"], "principal": "rule:read_only"}}
    with pytest.raises(DecisionRefused, match="person"):
        gate.decide_attested(pending.action_id, attestation=rule)
    # A record from another log is refused.
    with pytest.raises(DecisionRefused, match="approvals"):
        gate.decide_attested(
            pending.action_id, attestation={**machine, "signer": rec["signer"], "logbook": "demo_log"}
        )
    assert gate.get(pending.action_id).status == ActionStatus.PENDING


def test_lb_x3_a5_the_decision_binds_what_was_shown(gate, node_signer):
    action = held(gate)
    p = present_decision(action)
    # The card names the action and its parameters, with secret-shaped values hidden.
    assert "write_file" in p.rendered and "notes/a.txt" in p.rendered
    assert "s3cret-value" not in p.rendered
    # In the words the card uses: the decision itself, the action, why it was held, its parameters.
    for label in ("Decision", "Approve", "Action", "Why it was held", "a write needs a person", "Parameters"):
        assert label in p.rendered, label
    # The parameters change after the presentation: the signature is refused.
    changed = gate.get(action.action_id)
    changed.params = {**changed.params, "path": "elsewhere.txt"}
    gate.record(changed)
    with pytest.raises(AttestRefused, match="present"):
        sign_decision(p, node_signer)
    assert gate.get(action.action_id).status == ActionStatus.PENDING


def test_lb_x3_a6_there_is_no_agent_path(gate, tmp_path):
    reg = SkillRegistry()
    approval_skills.register_all(reg)
    for name in ("approval.approve", "approval.reject"):
        surfaces = set(reg.spec(name).surfaces)
        assert surfaces == {"cli", "skill_md"}, name
    # A chat or MCP caller that reaches the decision verb anyway is refused by the seam.
    action = held(gate)
    for surface in ("mcp", "agent_tool", "chat"):
        r = approval_skills.approve(
            {"action_id": action.action_id}, skill_ctx(tmp_path, answers=["sign"], surface=surface)
        )
        assert not r.ok, surface
    assert gate.get(action.action_id).status == ActionStatus.PENDING
    # An agent principal at the prompt is refused too.
    r = approval_skills.approve(
        {"action_id": action.action_id}, skill_ctx(tmp_path, handle="agent:helper", answers=["sign"])
    )
    assert not r.ok and gate.get(action.action_id).status == ActionStatus.PENDING

    # The token path (email, SMS) spends the token and decides nothing; it points to the card.
    tokens = approval_tokens.TokenStore(tmp_path / "tokens.json")
    secret = tokens.mint(action.action_id, channel="email")
    out = approval_tokens.redeem_and_decide(
        secret, gate, approving=True, decided_by=APPROVER, store=tokens
    )
    assert out is not None and out.status == ActionStatus.PENDING
    assert decisions.card_address(action.action_id).endswith(action.action_id)
    assert tokens.redeem(secret) is None  # spent


def test_lb_x3_a7_only_a_fresh_approver_signs(gate, node_signer):
    action = held(gate)
    for who in (approver(handle=OTHER, roles=("member",)), approver(handle=OTHER, roles=("admin",))):
        p = present_decision(action, who=who)
        with pytest.raises(AttestRefused, match="role"):
            sign_decision(p, node_signer, who=who)
    p = present_decision(action)
    with pytest.raises(GrantRefused) as exc:
        sign_decision(p, node_signer, minutes_ago=10)
    assert exc.value.code == "reauth_required"
    assert gate.get(action.action_id).status == ActionStatus.PENDING


def test_lb_x3_a8_with_the_flag_off_nothing_changes(gate, tmp_path, monkeypatch):
    monkeypatch.setenv("AXIOM_FLAG_APPROVALS_SIGNED", "off")
    action = held(gate)
    r = approval_skills.approve({"action_id": action.action_id}, skill_ctx(tmp_path))
    assert r.ok, r.errors
    after = gate.get(action.action_id)
    assert after.status == ActionStatus.APPROVED
    assert after.decided_by == APPROVER and after.attestation_id is None
    assert service.list_drafts(for_principal=APPROVER) == []
    other = held(gate)
    r = approval_skills.reject({"action_id": other.action_id, "reason": "no"}, skill_ctx(tmp_path))
    assert r.ok and gate.get(other.action_id).status == ActionStatus.REJECTED


def test_lb_x3_the_web_path_and_reconcile_after_a_lost_event(client, gate, node_signer):
    """The HTTP routes, and a signature whose gate write was lost is picked up by reconcile."""
    from axiom.extensions.builtins.attest.tests.test_api import OP2, as_

    action = held(gate)
    c = as_(client, OP2)
    pending = c.get("/api/v1/attest/approvals/pending")
    assert pending.status_code == 200, pending.text
    assert [a["action_id"] for a in pending.json()["pending"]] == [action.action_id]
    d = c.post(f"/api/v1/attest/approvals/{action.action_id}/decision", json={"decision": "approved"})
    assert d.status_code == 201, d.text
    draft = d.json()
    assert draft["logbook"] == "approvals" and draft["meaning"] == "approved"
    bad = c.post(f"/api/v1/attest/approvals/{action.action_id}/decision", json={"decision": "maybe"})
    assert bad.status_code == 422

    # Sign with the hook that moves the gate switched off, as if the process died after the commit.
    approvals.uninstall()
    p = service.present(draft["draft_id"], modality="screen")
    service.respond(p.presentation_id, principal=approver().principal, answer="sign", via="screen")
    rec = sign_decision(p, node_signer).record
    assert gate.get(action.action_id).status == ActionStatus.PENDING
    approvals.install(gate_factory=lambda: gate)
    moved = approvals.reconcile(gate)
    assert [a.action_id for a in moved] == [action.action_id]
    assert gate.get(action.action_id).attestation_id == rec["attestation_id"]
    assert approvals.reconcile(gate) == []  # idempotent
    assert json.dumps(rec["subjects"]) and re.match(r"^[0-9a-f-]{36}$", rec["attestation_id"])
