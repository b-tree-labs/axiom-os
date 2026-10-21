# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tests for action types and lifecycle."""


from axiom.infra.orchestrator.actions import (
    ACTION_REGISTRY,
    Action,
    ActionCategory,
    ActionStatus,
    create_action,
)


class TestAction:
    """Test Action data model and lifecycle."""

    def test_default_status_pending(self):
        a = Action(name="test")
        assert a.status == ActionStatus.PENDING

    def test_approve(self):
        a = Action(name="test")
        a.approve()
        assert a.status == ActionStatus.APPROVED

    def test_reject(self):
        a = Action(name="test")
        a.reject("not now")
        assert a.status == ActionStatus.REJECTED
        assert a.error == "not now"

    def test_complete(self):
        a = Action(name="test")
        a.approve()
        a.complete({"output": "done"})
        assert a.status == ActionStatus.COMPLETED
        assert a.result == {"output": "done"}
        assert a.completed_at is not None

    def test_fail(self):
        a = Action(name="test")
        a.approve()
        a.fail("something broke")
        assert a.status == ActionStatus.FAILED
        assert a.error == "something broke"

    def test_roundtrip(self):
        a = Action(name="doc.publish", params={"source": "foo.md"})
        d = a.to_dict()
        a2 = Action.from_dict(d)
        assert a2.name == "doc.publish"
        assert a2.params == {"source": "foo.md"}
        assert a2.action_id == a.action_id

    def test_unique_ids(self):
        a1 = Action(name="test")
        a2 = Action(name="test")
        assert a1.action_id != a2.action_id


class TestCreateAction:
    """Test action factory with registry lookup."""

    def test_read_action(self):
        a = create_action("query_docs")
        assert a.category == ActionCategory.READ

    def test_write_action(self):
        a = create_action("doc_publish", {"source": "foo.md"})
        assert a.category == ActionCategory.WRITE

    def test_unknown_defaults_to_write(self):
        a = create_action("something_unknown")
        assert a.category == ActionCategory.WRITE


class TestActionRegistry:
    """Test the pre-defined action registry."""

    def test_read_actions_exist(self):
        reads = [k for k, v in ACTION_REGISTRY.items() if v == ActionCategory.READ]
        assert "query_docs" in reads
        assert "sense_status" in reads
        assert "list_providers" in reads

    def test_write_actions_exist(self):
        writes = [k for k, v in ACTION_REGISTRY.items() if v == ActionCategory.WRITE]
        assert "doc_publish" in writes
        assert "sense_ingest" in writes
        assert "write_inbox_note" in writes
