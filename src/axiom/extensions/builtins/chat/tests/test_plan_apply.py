# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Plan, then apply: a destructive step runs only against an approval of exactly what was planned.

Real approval queue (a file store in a temp dir), real ``ApprovalGate``; the step being guarded is a function
that records that it ran, so every refusal is checked by "it did not run".
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from axiom.extensions.builtins.chat import plan_apply as pa
from axiom.infra.orchestrator.approval import ApprovalGate
from axiom.infra.orchestrator.approval_store import FileActionStore

KIND = "test.destroy"
SUBJECT = {"paths": ["a.txt", "b.txt"], "digest": "abc123"}


@pytest.fixture
def store(tmp_path):
    return FileActionStore(tmp_path / "approvals.json")


class Step:
    """The guarded destructive step: counts how many times it really ran."""

    def __init__(self, current=None):
        self.ran = 0
        self.current = SUBJECT if current is None else current

    def recompute(self):
        return self.current

    def execute(self, subject):
        self.ran += 1
        return "done"


def _approve(store, plan, who="@ben:local"):
    ApprovalGate(store).approve(plan.approval_id, decided_by=who)


def _apply(store, plan, step, **kw):
    return pa.apply(
        kw.pop("plan_hash", plan.plan_hash), plan.approval_id, kind=KIND,
        recompute=step.recompute, execute=step.execute, store=store, **kw,
    )


def test_hash_is_content_not_order_or_spacing():
    assert pa.plan_hash(KIND, {"a": 1, "b": [1, 2]}) == pa.plan_hash(KIND, {"b": [1, 2], "a": 1})
    assert pa.plan_hash(KIND, {"a": 1}) != pa.plan_hash(KIND, {"a": 2})
    assert pa.plan_hash("x", {"a": 1}) != pa.plan_hash("y", {"a": 1})


def test_propose_changes_nothing_and_holds_the_plan_in_the_queue(store):
    step = Step()
    plan = pa.propose(KIND, SUBJECT, summary="destroy two files", store=store)
    held = ApprovalGate(store).pending()
    assert [a.action_id for a in held] == [plan.approval_id]
    assert held[0].params["plan_hash"] == plan.plan_hash
    assert step.ran == 0


def test_an_approved_plan_runs_exactly_once(store):
    step = Step()
    plan = pa.propose(KIND, SUBJECT, summary="s", store=store)
    _approve(store, plan)
    assert _apply(store, plan, step) == "done"
    assert step.ran == 1


def test_unapproved_is_refused_and_names_how_to_approve(store):
    step = Step()
    plan = pa.propose(KIND, SUBJECT, summary="s", store=store)
    with pytest.raises(pa.ApprovalRequired) as err:
        _apply(store, plan, step)
    assert plan.approval_id in str(err.value) and "approve ok" in str(err.value)
    assert step.ran == 0


def test_a_rejected_approval_is_refused(store):
    step = Step()
    plan = pa.propose(KIND, SUBJECT, summary="s", store=store)
    ApprovalGate(store).reject(plan.approval_id, "no", decided_by="@ben:local")
    with pytest.raises(pa.ApprovalRejected):
        _apply(store, plan, step)
    assert step.ran == 0


def test_a_mismatched_hash_is_refused(store):
    step = Step()
    plan = pa.propose(KIND, SUBJECT, summary="s", store=store)
    _approve(store, plan)
    with pytest.raises(pa.PlanMismatch):
        _apply(store, plan, step, plan_hash="0" * 64)
    assert step.ran == 0


def test_approval_for_one_plan_cannot_apply_another_kind(store):
    step = Step()
    plan = pa.propose("test.other", SUBJECT, summary="s", store=store)
    _approve(store, plan)
    with pytest.raises(pa.PlanMismatch):
        _apply(store, plan, step)
    assert step.ran == 0


def test_the_world_moving_since_approval_is_refused(store):
    step = Step(current={"paths": ["a.txt", "b.txt", "c.txt"], "digest": "abc123"})
    plan = pa.propose(KIND, SUBJECT, summary="s", store=store)
    _approve(store, plan)
    with pytest.raises(pa.PlanMismatch, match="changed since"):
        _apply(store, plan, step)
    assert step.ran == 0


def test_replay_is_refused(store):
    step = Step()
    plan = pa.propose(KIND, SUBJECT, summary="s", store=store)
    _approve(store, plan)
    _apply(store, plan, step)
    with pytest.raises(pa.PlanReplayed):
        _apply(store, plan, step)
    assert step.ran == 1


def test_a_failed_step_is_not_retried_on_the_same_approval(store):
    plan = pa.propose(KIND, SUBJECT, summary="s", store=store)
    _approve(store, plan)
    calls = []

    def boom(subject):
        calls.append(1)
        raise OSError("disk")

    with pytest.raises(OSError):
        pa.apply(plan.plan_hash, plan.approval_id, kind=KIND, recompute=lambda: SUBJECT, execute=boom, store=store)
    with pytest.raises(pa.PlanReplayed):
        pa.apply(plan.plan_hash, plan.approval_id, kind=KIND, recompute=lambda: SUBJECT, execute=boom, store=store)
    assert calls == [1]


def test_an_expired_plan_is_refused(store):
    step = Step()
    created = datetime.now(UTC)
    plan = pa.propose(KIND, SUBJECT, summary="s", ttl_seconds=60, store=store, now=created)
    _approve(store, plan)
    with pytest.raises(pa.PlanExpired):
        _apply(store, plan, step, now=created + timedelta(seconds=61))
    assert step.ran == 0
    # inside the window the same approval works
    assert _apply(store, plan, step, now=created + timedelta(seconds=30)) == "done"


def test_an_unknown_approval_id_is_refused(store):
    step = Step()
    with pytest.raises(pa.ApprovalRequired):
        pa.apply("f" * 64, "no-such-id", kind=KIND, recompute=step.recompute, execute=step.execute, store=store)
    assert step.ran == 0


def test_a_plan_subject_never_carries_a_credential(store):
    with pytest.raises(ValueError, match="credential"):
        pa.propose(KIND, {"note": "aws key AKIAIOSFODNN7EXAMPLE"}, summary="s", store=store)
    assert ApprovalGate(store).pending() == []


def test_an_approval_must_name_who_approved(store):
    plan = pa.propose(KIND, SUBJECT, summary="s", store=store)
    with pytest.raises(ValueError):
        pa.approve_in_person(plan, "", store=store)


def test_a_crash_mid_step_is_not_retried_on_the_same_approval(store):
    """A process that dies while the step runs leaves the record claimed, so a restart cannot run it twice."""
    plan = pa.propose(KIND, SUBJECT, summary="s", store=store)
    _approve(store, plan)
    calls = []

    def die(subject):
        calls.append(1)
        raise KeyboardInterrupt  # not an Exception: nothing below the step gets to tidy the record

    with pytest.raises(KeyboardInterrupt):
        pa.apply(plan.plan_hash, plan.approval_id, kind=KIND, recompute=lambda: SUBJECT, execute=die, store=store)
    with pytest.raises((pa.PlanReplayed, KeyboardInterrupt)) as err:
        pa.apply(plan.plan_hash, plan.approval_id, kind=KIND, recompute=lambda: SUBJECT, execute=die, store=store)
    assert isinstance(err.value, pa.PlanReplayed), "the step ran a second time after a crash"
    assert "running" in str(err.value)
    assert calls == [1]


def test_a_second_apply_started_while_the_first_is_running_is_refused(store):
    plan = pa.propose(KIND, SUBJECT, summary="s", store=store)
    _approve(store, plan)
    refusals = []

    def reentrant(subject):
        try:
            pa.apply(plan.plan_hash, plan.approval_id, kind=KIND, recompute=lambda: SUBJECT, execute=reentrant, store=store)
        except pa.PlanReplayed as exc:
            refusals.append(str(exc))
        return "outer"

    assert pa.apply(plan.plan_hash, plan.approval_id, kind=KIND, recompute=lambda: SUBJECT, execute=reentrant, store=store) == "outer"
    assert len(refusals) == 1
