# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tests for the approval gate."""


from axiom.infra.orchestrator.actions import (
    ActionStatus,
    create_action,
)
from axiom.infra.orchestrator.approval import ApprovalGate


class TestApprovalGate:
    """Test human-in-the-loop approval logic."""

    def test_read_auto_approved(self):
        gate = ApprovalGate()
        action = create_action("query_docs")
        gate.submit(action)
        assert action.status == ActionStatus.APPROVED

    def test_write_stays_pending(self):
        gate = ApprovalGate()
        action = create_action("doc_publish", {"source": "foo.md"})
        gate.submit(action)
        assert action.status == ActionStatus.PENDING

    def test_approve_pending(self):
        gate = ApprovalGate()
        action = create_action("doc_publish")
        gate.submit(action)

        gate.approve(action.action_id)
        assert action.status == ActionStatus.APPROVED

    def test_reject_pending(self):
        gate = ApprovalGate()
        action = create_action("doc_publish")
        gate.submit(action)

        gate.reject(action.action_id, "Not ready")
        assert action.status == ActionStatus.REJECTED
        assert action.error == "Not ready"

    def test_pending_list(self):
        gate = ApprovalGate()
        a1 = create_action("doc_publish")
        a2 = create_action("query_docs")  # read → auto-approved
        a3 = create_action("sense_ingest")

        gate.submit(a1)
        gate.submit(a2)
        gate.submit(a3)

        pending = gate.pending()
        assert len(pending) == 2
        assert a1 in pending
        assert a3 in pending
        assert a2 not in pending  # Auto-approved

    def test_get_action(self):
        gate = ApprovalGate()
        action = create_action("doc_publish")
        gate.submit(action)
        assert gate.get(action.action_id) is action

    def test_get_nonexistent_returns_none(self):
        gate = ApprovalGate()
        assert gate.get("nonexistent") is None

    def test_all_actions(self):
        gate = ApprovalGate()
        gate.submit(create_action("query_docs"))
        gate.submit(create_action("doc_publish"))
        assert len(gate.all_actions()) == 2
