# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""S15 Reject at once: a held action rejected in one step, with no confirm card (held card D1).

The held-action card design (appkit ``docs/design/proposed-2026-11-13/held-card/``, decision D1,
approved 2026-11-13) makes Reject happen at once: rejecting is safe to undo (the agent can ask
again, DC-10), so it is not presented on the confirm card. It is still a person's signed decision:
``POST /gate/approvals/{action_id}/reject`` drafts the rejection, presents it, records the answer,
mints the grant from the gate session and signs, in one call, so the record and the gate's move are
exactly what the confirm card's path makes. Only a gate session reaches it (ADR-262): never a key.
"""

from __future__ import annotations

import pytest

from .conftest import ANN, BO, SITE, as_, as_helper

APPROVER = BO


@pytest.fixture
def gate(world, monkeypatch):
    """The approvals log, the decision seam and a durable gate; BO holds ``approver``."""
    from axiom.extensions.builtins.attest import approvals, registry, roles
    from axiom.infra.orchestrator.approval import ApprovalGate
    from axiom.infra.orchestrator.approval_store import FileActionStore

    monkeypatch.setenv("AXIOM_FLAG_APPROVALS_SIGNED", "on")
    registry.register_file(approvals.LOGBOOK_FILE)
    g = ApprovalGate(FileActionStore(world.state_dir / "orchestrator" / "approvals.json"))
    approvals.install(gate_factory=lambda: g)
    roles.grant(APPROVER.handle, SITE, "approver", by="@admin:example-site", state_dir=world.state_dir)
    yield g
    approvals.uninstall()


def _held(g, name="write_file"):
    from axiom.infra.orchestrator.actions import create_action

    action = create_action(name, {"path": "notes/a.txt", "mode": "append"})
    action.reason = "a write needs a person"
    return g.submit(action)


def _reject(web, p, action_id, **kw):
    return as_(web, p, **kw).post(f"/gate/approvals/{action_id}/reject", json={})


def _open_drafts(handle):
    from axiom.extensions.builtins.attest import service

    return service.list_drafts(for_principal=handle, status="open")


@pytest.mark.acceptance("HC-D1-A1")
def test_hc_d1_a1_an_approver_rejects_in_one_call_and_it_is_a_signed_decision(world, web, gate):
    from axiom.extensions.builtins.attest import service
    from axiom.infra.orchestrator.actions import ActionStatus

    action = _held(gate)
    r = _reject(web, APPROVER, action.action_id)
    assert r.status_code == 201, r.text
    body = r.json()
    rec = body["record"]
    assert (rec["logbook"], rec["entry_type"], rec["meaning"]) == ("approvals", "ACTION_DECISION", "rejected")
    assert rec["subjects"] == [{"kind": "action", "value": action.action_id}]
    assert body["decision"] == "rejected" and body["decided_by"] == APPROVER.handle
    assert body["recorded_at"] == rec["recorded_at"]
    after = gate.get(action.action_id)
    assert after.status == ActionStatus.REJECTED and after.attestation_id == rec["attestation_id"]
    # Exactly the record the confirm card's path makes, on the one approvals chain.
    assert [x["attestation_id"] for x in service.records(SITE, "approvals")] == [rec["attestation_id"]]
    assert _open_drafts(APPROVER.handle) == []


@pytest.mark.acceptance("HC-D1-A2")
def test_hc_d1_a2_a_person_who_is_not_an_approver_is_refused_and_nothing_moves(world, web, gate):
    from axiom.extensions.builtins.attest import service
    from axiom.infra.orchestrator.actions import ActionStatus

    action = _held(gate)
    r = _reject(web, ANN, action.action_id)
    assert r.status_code == 403, r.text
    assert gate.get(action.action_id).status == ActionStatus.PENDING
    assert service.records(SITE, "approvals") == []
    assert _open_drafts(ANN.handle) == []


@pytest.mark.acceptance("HC-D1-A3")
def test_hc_d1_a3_a_stale_sign_in_asks_to_sign_in_again_and_leaves_no_draft(world, web, gate):
    from axiom.extensions.builtins.attest import service
    from axiom.infra.orchestrator.actions import ActionStatus

    action = _held(gate)
    r = _reject(web, APPROVER, action.action_id, auth_age=600)  # the type needs a sign-in within 5 min
    assert r.status_code == 403
    assert r.json()["code"] == "reauth_required" and r.json()["max_age"] == 300
    assert gate.get(action.action_id).status == ActionStatus.PENDING
    assert service.records(SITE, "approvals") == []
    assert _open_drafts(APPROVER.handle) == []


@pytest.mark.acceptance("HC-D1-A4")
def test_hc_d1_a4_an_unknown_or_settled_action_is_refused(world, web, gate):
    assert _reject(web, APPROVER, "nope").status_code == 404
    action = _held(gate)
    assert _reject(web, APPROVER, action.action_id).status_code == 201
    again = _reject(web, APPROVER, action.action_id)
    assert again.status_code == 409 and again.json()["code"] == "settled"


@pytest.mark.acceptance("HC-D1-A5")
def test_hc_d1_a5_only_a_gate_session_reaches_it_never_a_key(world, web, gate, helper):
    from axiom.infra.orchestrator.actions import ActionStatus

    action = _held(gate)
    assert _reject(web, None, action.action_id).status_code == 401
    keyed = web.post(f"/gate/approvals/{action.action_id}/reject", json={}, headers=as_helper(web))
    assert keyed.status_code == 401
    assert gate.get(action.action_id).status == ActionStatus.PENDING


@pytest.mark.acceptance("HC-D1-A6")
def test_hc_d1_a6_the_pending_list_names_where_to_reject(world, web, gate):
    action = _held(gate)
    pending = as_(web, APPROVER).get("/api/v1/attest/approvals/pending").json()["pending"]
    assert pending[0]["reject"] == f"/gate/approvals/{action.action_id}/reject"
