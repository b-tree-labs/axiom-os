# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tests for the durable approval store.

The properties worth pinning here are all about what happens when things go
wrong, because the store's whole reason to exist is that the process which
proposed an action is often not the process that answers for it.
"""

from datetime import UTC, datetime, timedelta

import pytest

from axiom.infra.orchestrator.actions import ActionStatus, create_action
from axiom.infra.orchestrator.approval import ApprovalGate
from axiom.infra.orchestrator.approval_store import (
    ApprovalStoreUnavailable,
    FileActionStore,
    InMemoryActionStore,
    default_approval_path,
)


def _write_action():
    return create_action("doc_publish", {"source": "docs/prds/prd_foo.md"})


class TestSurvivesTheProcess:
    """The point of the whole exercise."""

    def test_a_pending_action_outlives_the_gate_that_proposed_it(self, tmp_path):
        path = tmp_path / "approvals.json"

        proposer = ApprovalGate(FileActionStore(path))
        action_id = proposer.submit(_write_action()).action_id
        del proposer

        # A different gate, standing in for a different process entirely.
        approver = ApprovalGate(FileActionStore(path))
        assert [a.action_id for a in approver.pending()] == [action_id]
        assert approver.approve(action_id).status == ActionStatus.APPROVED

        assert ApprovalGate(FileActionStore(path)).pending() == []

    def test_the_returned_action_is_authoritative_not_the_submitted_one(self, tmp_path):
        """The identity contract the class docstring promises.

        With the old dict store this assertion was vacuous, because both names
        pointed at one object. The durable store is what makes it mean
        something, and what makes getting it wrong dangerous: the stale
        reference reads as PENDING, which looks like a working gate right up
        until someone asks why an approved action never ran.
        """
        gate = ApprovalGate(FileActionStore(tmp_path / "approvals.json"))
        submitted = gate.submit(_write_action())

        approved = gate.approve(submitted.action_id)

        assert approved is not submitted, "a durable store cannot return the same object"
        assert approved.status == ActionStatus.APPROVED
        assert submitted.status == ActionStatus.PENDING, "the passed-in object is a snapshot"
        assert gate.get(submitted.action_id).status == ActionStatus.APPROVED


class TestFailsLoudlyNotEmptily:
    """An unreadable store must never present as an empty queue.

    The sibling precedent, ``FileDedupLog``, degrades the other way on purpose:
    losing suppression is noisy, losing an alert is fatal. Here the polarity
    flips. An empty queue is technically fail-closed, since nothing runs, but it
    presents as "there is nothing to approve", which is indistinguishable from a
    healthy idle system.
    """

    def test_a_corrupt_store_raises_rather_than_reporting_nothing_to_approve(self, tmp_path):
        """Regression: ``LockedJsonFile.read()`` swallows ``JSONDecodeError``.

        It catches the decode error and returns ``{}``, which is right for a
        cache and wrong for this. Without an explicit check, the single likeliest
        real-world failure (a truncated write, a half-synced file) would sail
        straight through the guard this module is built around.
        """
        path = tmp_path / "approvals.json"
        path.write_text('{"a1": {"action_id": "a1", "sta')  # truncated mid-write

        with pytest.raises(ApprovalStoreUnavailable, match="did not parse"):
            FileActionStore(path).all()

    def test_a_store_holding_the_wrong_shape_raises(self, tmp_path):
        path = tmp_path / "approvals.json"
        path.write_text('["not", "an", "object"]')

        with pytest.raises(ApprovalStoreUnavailable, match="not a JSON object"):
            FileActionStore(path).all()

    def test_a_genuinely_empty_store_is_not_an_error(self, tmp_path):
        """Absent and unreadable are different facts and must stay different."""
        assert FileActionStore(tmp_path / "nothing-here.json").all() == []
        empty = tmp_path / "empty.json"
        empty.write_text("{}")
        assert FileActionStore(empty).all() == []


class TestExpiry:
    """Durability without expiry is its own hazard."""

    def _stale(self, tmp_path, age_hours):
        path = tmp_path / "approvals.json"
        store = FileActionStore(path, ttl_hours=72.0)
        action = _write_action()
        action.created_at = (datetime.now(UTC) - timedelta(hours=age_hours)).isoformat()
        store.put(action)
        return store, action.action_id

    def test_an_action_older_than_the_ttl_is_no_longer_pending(self, tmp_path):
        store, action_id = self._stale(tmp_path, 100)
        assert store.get(action_id).status == ActionStatus.REJECTED

    def test_expiry_says_the_answer_never_came_not_that_someone_said_no(self, tmp_path):
        """A reader asking why this did not run deserves the distinction."""
        store, action_id = self._stale(tmp_path, 100)
        assert "without an answer" in store.get(action_id).error

    def test_an_action_inside_the_window_is_untouched(self, tmp_path):
        store, action_id = self._stale(tmp_path, 1)
        assert store.get(action_id).status == ActionStatus.PENDING

    def test_an_expired_action_cannot_be_approved(self, tmp_path):
        """Approving a closed window is approving against a world that moved."""
        store, action_id = self._stale(tmp_path, 100)
        gate = ApprovalGate(store)
        assert gate.approve(action_id).status == ActionStatus.REJECTED


class TestDefaults:
    def test_the_gate_is_in_memory_unless_asked_otherwise(self, tmp_path, monkeypatch):
        """Durable-by-default wrote to the developer's real ``~/.axi`` on import.

        Constructing a gate is not consent to touch the home directory.
        """
        monkeypatch.setenv("AXI_STATE_DIR", str(tmp_path))
        ApprovalGate().submit(_write_action())
        assert not (tmp_path / "orchestrator").exists()

    def test_durable_true_opts_in_without_naming_a_store(self, tmp_path, monkeypatch):
        monkeypatch.setenv("AXI_STATE_DIR", str(tmp_path))
        ApprovalGate(durable=True).submit(_write_action())
        assert (tmp_path / "orchestrator" / "approvals.json").exists()

    def test_the_default_path_follows_the_state_dir(self, tmp_path, monkeypatch):
        monkeypatch.setenv("AXI_STATE_DIR", str(tmp_path))
        assert default_approval_path() == tmp_path / "orchestrator" / "approvals.json"


class TestPurge:
    def test_resolved_actions_are_dropped_and_pending_ones_kept(self, tmp_path):
        """The queue is a work list. The receipt chain is the audit log."""
        store = FileActionStore(tmp_path / "approvals.json")
        gate = ApprovalGate(store)
        resolved = gate.submit(_write_action()).action_id
        kept = gate.submit(_write_action()).action_id
        gate.approve(resolved)

        assert store.purge_resolved() == 1
        assert [a.action_id for a in store.all()] == [kept]


class TestInMemoryStoreStillWorks:
    def test_it_is_the_old_behaviour_named(self):
        store = InMemoryActionStore()
        gate = ApprovalGate(store)
        action = gate.submit(_write_action())
        assert gate.approve(action.action_id) is action, "no serialization, same object"


class TestOneQueueNotTwo:
    """What holds an approval and what reads it must agree on the path.

    They did not. ``SkillContext.state_dir`` is ``get_user_state_dir()``
    (``~/.axi``) in chat and every CLI verb, while ``default_approval_path``
    hand-rolled ``~/.axi/state``. An approval held through a skill context
    landed one directory away from where ``axi approve`` looked, so the queue
    read as empty — the exact failure this store exists to prevent.
    """

    def test_the_cli_and_the_default_path_agree(self, tmp_path, monkeypatch):
        monkeypatch.setenv("AXI_STATE_DIR", str(tmp_path))
        from axiom.cli.approve import _state_dir

        assert (
            _state_dir() / "orchestrator" / "approvals.json" == default_approval_path()
        )

    def test_a_skill_context_hold_is_visible_to_the_cli(self, tmp_path, monkeypatch):
        """End to end, through the two functions that actually disagreed."""
        monkeypatch.setenv("AXI_STATE_DIR", str(tmp_path))
        from axiom.infra.paths import get_user_state_dir

        held = ApprovalGate(
            FileActionStore(get_user_state_dir() / "orchestrator" / "approvals.json")
        ).submit(_write_action())

        seen = ApprovalGate(FileActionStore(default_approval_path())).pending()
        assert [a.action_id for a in seen] == [held.action_id]

    def test_it_honours_the_platform_override_not_a_private_one(
        self, tmp_path, monkeypatch
    ):
        """``AXI_STATE_DIR`` is the documented variable; ``AXIOM_STATE_DIR`` was mine."""
        monkeypatch.setenv("AXI_STATE_DIR", str(tmp_path))
        monkeypatch.setenv("AXIOM_STATE_DIR", str(tmp_path / "wrong"))

        assert default_approval_path().is_relative_to(tmp_path)
        assert not default_approval_path().is_relative_to(tmp_path / "wrong")

    def test_the_path_does_not_hardcode_a_consumer_name(self, tmp_path, monkeypatch):
        """Branding owns the directory name; the platform must not bake one in."""
        monkeypatch.setenv("AXI_STATE_DIR", str(tmp_path / "someproduct"))
        assert ".axi" not in str(default_approval_path())
