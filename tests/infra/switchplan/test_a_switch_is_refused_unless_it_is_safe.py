# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""ADR-197 D3: the runner refuses the wrong order, whatever the domain does.

No database here: file ledger, file lease, callable/file/command probes. Each test
names one property the runner must hold on its own.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from axiom.infra import change_intent
from axiom.infra.switchplan.errors import (
    BudgetUnmeetable,
    GateNotPassed,
    RollbackFailed,
    SwitchBusy,
    SwitchRefused,
)
from axiom.infra.switchplan.ledger import FileLedger
from axiom.infra.switchplan.lock import SingleFlight
from axiom.infra.switchplan.plan import LockBudget, SwitchPlan
from axiom.infra.switchplan.probes import Probe

OWNER = "@ben.booth:axiom"


class Hooks:
    """A recording domain: a 'pointer' file that names the live version."""

    def __init__(self, tmp_path, *, healthy=True, commit_raises=False, tail_s=None, with_rollback=True, probes=None):
        self.ptr = tmp_path / "live"
        self.ptr.write_text("old")
        self.calls: list[str] = []
        self.healthy = healthy
        self.commit_raises = commit_raises
        self.tail_s = tail_s
        self.change_path = None
        self.seen_intent_at_prepare = None
        self._probes = probes
        if not with_rollback:
            self.rollback = None  # type: ignore[assignment]

    def prepare(self, ctx):
        self.calls.append("prepare")
        self.seen_intent_at_prepare = change_intent.has_intent(ctx.record.change_id, path=self.change_path)

    def probe(self, ctx):
        if self._probes is not None:
            return self._probes
        return [
            Probe("new-is-healthy", "callable", fn=lambda c: (self.healthy, "ok" if self.healthy else "refused to answer"), when="pre"),
            Probe("pointer-says-new", "file", {"path": str(self.ptr), "contains": "new"}, when="post"),
        ]

    def commit(self, ctx, scope):
        self.calls.append("commit")
        if self.commit_raises:
            raise RuntimeError("the switch itself failed")
        with scope.holding():
            self.ptr.write_text("new")
        return "pointer moved"

    def rollback(self, ctx):
        self.calls.append("rollback")
        self.ptr.write_text("old")

    def retire(self, ctx):
        self.calls.append("retire")
        return "old removed"

    def tail_estimate(self, ctx):
        return self.tail_s or 0.0


def plan_for(tmp_path, hooks, **kw):
    cp = tmp_path / "changes.jsonl"
    hooks.change_path = cp
    kw.setdefault("owner", OWNER)
    kw.setdefault("lock", SingleFlight(f"t:{tmp_path.name}", lease_dir=tmp_path / "leases"))
    return SwitchPlan(kind="pointer", subject="live", hooks=hooks, ledger=FileLedger(tmp_path / "ledger"),
                      change_path=cp, **kw)


def test_the_change_record_is_on_disk_before_prepare_acts(tmp_path):
    h = Hooks(tmp_path)
    plan_for(tmp_path, h).prepare()
    assert h.seen_intent_at_prepare is True


def test_commit_is_refused_when_the_gate_never_ran(tmp_path):
    h = Hooks(tmp_path)
    p = plan_for(tmp_path, h)
    p.prepare()
    with pytest.raises(GateNotPassed, match="gate has not run"):
        p.commit()
    assert "commit" not in h.calls and h.ptr.read_text() == "old"


def test_commit_is_refused_when_the_gate_failed_and_nothing_moves(tmp_path):
    h = Hooks(tmp_path, healthy=False)
    p = plan_for(tmp_path, h)
    p.prepare()
    with pytest.raises(GateNotPassed, match="refused to answer"):
        p.prove()
    with pytest.raises(GateNotPassed):
        p.commit()
    assert h.ptr.read_text() == "old" and "commit" not in h.calls


def test_a_probe_that_cannot_be_evaluated_fails(tmp_path):
    h = Hooks(tmp_path, probes=[Probe("boom", "callable", fn=lambda c: 1 / 0)])
    p = plan_for(tmp_path, h)
    with pytest.raises(GateNotPassed, match="could not be evaluated"):
        p.prove()


def test_commit_is_refused_when_the_change_record_is_missing(tmp_path):
    h = Hooks(tmp_path)
    p = plan_for(tmp_path, h)
    p.prepare()
    p.prove()
    (tmp_path / "changes.jsonl").unlink()
    with pytest.raises(SwitchRefused, match="change record was not written"):
        p.commit()
    assert "commit" not in h.calls


def test_changing_the_probes_after_prove_invalidates_the_gate(tmp_path):
    h = Hooks(tmp_path)
    p = plan_for(tmp_path, h)
    p.prepare()
    p.prove()
    h._probes = [Probe("weaker", "callable", fn=lambda c: True)]
    with pytest.raises(GateNotPassed, match="probes changed"):
        p.commit()


def test_no_probes_or_no_rollback_is_refused(tmp_path):
    with pytest.raises(GateNotPassed, match="no probes"):
        plan_for(tmp_path, Hooks(tmp_path, probes=[])).prove()
    sub = tmp_path / "b"
    sub.mkdir()
    with pytest.raises(SwitchRefused, match="no rollback hook"):
        plan_for(sub, Hooks(sub, with_rollback=False)).prove()


def test_a_data_switch_must_read_through_the_original_name(tmp_path):
    h = Hooks(tmp_path)
    with pytest.raises(GateNotPassed, match="relation_read"):
        plan_for(tmp_path, h, data=True).prove()


def test_a_commit_that_cannot_meet_its_budget_never_takes_the_lock(tmp_path):
    h = Hooks(tmp_path, tail_s=41.0)
    p = plan_for(tmp_path, h, budget=LockBudget(max_hold_s=2.0))
    p.prepare()
    p.prove()
    with pytest.raises(BudgetUnmeetable, match="41.0 s.*budget is 2 s"):
        p.commit()
    assert "commit" not in h.calls
    h.tail_s = 0.4  # a catch-up pass ran
    p.commit()
    assert h.ptr.read_text() == "new"


def test_the_default_budget_is_small_and_a_breach_is_recorded(tmp_path):
    assert LockBudget().max_hold_s == 2.0
    h = Hooks(tmp_path)
    p = plan_for(tmp_path, h, budget=LockBudget(max_hold_s=0.0))
    p.run()
    assert p.record.holds[-1]["breach"] is True
    assert any(e["step"] == "budget_breach" for e in p.record.events)


def test_a_reclaim_retire_needs_an_owner(tmp_path):
    with pytest.raises(SwitchRefused, match="needs an owner"):
        plan_for(tmp_path, Hooks(tmp_path), owner=None).prove()


def test_a_failed_commit_rolls_back_and_the_change_closes_failed(tmp_path):
    h = Hooks(tmp_path, commit_raises=True)
    p = plan_for(tmp_path, h)
    p.prepare()
    p.prove()
    with pytest.raises(RuntimeError, match="the switch itself failed"):
        p.commit()
    assert h.calls[-1] == "rollback" and p.record.phase == "rolled_back"
    assert change_intent.changes(path=tmp_path / "changes.jsonl")[0].outcome == "failed"


def test_a_rollback_that_fails_is_loud(tmp_path):
    h = Hooks(tmp_path, commit_raises=True)
    h.rollback = lambda ctx: (_ for _ in ()).throw(OSError("disk"))  # type: ignore[method-assign]
    p = plan_for(tmp_path, h)
    p.prepare()
    p.prove()
    with pytest.raises(RollbackFailed, match="needs a person"):
        p.commit()
    assert p.record.phase == "failed"


def test_commit_proposes_a_retire_task_with_owner_due_and_check(tmp_path):
    h = Hooks(tmp_path)
    p = plan_for(tmp_path, h)
    p.run()
    t = p.record.retire_task
    assert t["owner"] == OWNER and t["due_at"] and t["check"] == ["pointer-says-new"] and t["status"] == "open"


def test_follow_ups_seam_is_called_with_the_task(tmp_path):
    seen = {}

    class F:
        def propose(self, **kw):
            seen.update(kw)
            return "fu-1"

    p = plan_for(tmp_path, Hooks(tmp_path), follow_ups=F())
    p.run()
    assert seen["owner"] == OWNER and p.record.retire_task["followup_id"] == "fu-1"


def test_a_soak_fault_rolls_back_with_no_person_present(tmp_path):
    h = Hooks(tmp_path)
    faults: list[str] = []
    p = plan_for(tmp_path, h, soak=timedelta(hours=1), fault_source=lambda: faults)
    p.run()
    assert p.soak_check() == "soaking"
    faults.append("latched: readers failing")
    assert p.soak_check() == "rolled_back"
    assert h.ptr.read_text() == "old" and p.record.retire_task["status"] == "cancelled"
    outcomes = [c.outcome for c in change_intent.changes(path=tmp_path / "changes.jsonl")]
    assert outcomes == ["switched", "rolled_back"]


def test_a_failing_post_probe_in_the_soak_rolls_back(tmp_path):
    h = Hooks(tmp_path)
    p = plan_for(tmp_path, h, soak=timedelta(hours=1))
    p.run()
    h.ptr.write_text("old")  # the world drifts back: the post probe no longer holds
    assert p.soak_check() == "rolled_back"


def test_retire_that_cannot_be_undone_is_explicit(tmp_path):
    h = Hooks(tmp_path)
    p = plan_for(tmp_path, h)  # no soak: finished at once
    p.run()
    with pytest.raises(SwitchRefused, match="approver"):
        p.retire(approver="")
    with pytest.raises(SwitchRefused, match="approver"):
        p.retire(approver="@system")
    with pytest.raises(SwitchRefused, match="soak has not finished"):
        p.retire(approver=OWNER)
    assert p.soak_check() == "soaked"
    h.ptr.write_text("old")
    with pytest.raises(GateNotPassed, match="verify before retire"):
        p.retire(approver=OWNER)
    h.ptr.write_text("new")
    p.retire(approver=OWNER)
    assert p.record.phase == "retired" and p.record.approver == OWNER and p.record.retire_task["status"] == "done"
    assert "retire" in h.calls


def test_a_release_retire_follows_a_passing_soak_with_no_approver(tmp_path):
    h = Hooks(tmp_path)
    p = plan_for(tmp_path, h, retire="release", owner=None)
    p.run()
    assert p.soak_check() == "retired" and "retire" in h.calls and p.record.retire_task is None


def test_nothing_can_be_retired_or_committed_twice(tmp_path):
    p = plan_for(tmp_path, Hooks(tmp_path))
    p.run()
    with pytest.raises(SwitchRefused, match="already committed"):
        p.commit()


def test_two_runs_at_once_one_is_refused(tmp_path):
    h = Hooks(tmp_path)
    p = plan_for(tmp_path, h)
    holder = SingleFlight(p.lock.name, lease_dir=tmp_path / "leases")
    assert holder.acquire()
    try:
        with pytest.raises(SwitchBusy, match="nothing was started"):
            p.prepare()
        assert h.calls == []
    finally:
        holder.release()
    p.prepare()  # the lock is free again
    assert h.calls == ["prepare"]


def test_a_resumed_plan_continues_from_the_ledger(tmp_path):
    h = Hooks(tmp_path)
    p = plan_for(tmp_path, h)
    p.prepare()
    p.prove()
    rid = p.record.id
    # a new process: a new object over the same ledger and change record
    p2 = SwitchPlan(kind="pointer", subject="live", hooks=h, ledger=FileLedger(tmp_path / "ledger"), owner=OWNER,
                    change_path=tmp_path / "changes.jsonl", record_id=rid)
    assert p2.record.phase == "proved"
    p2.commit()
    assert h.ptr.read_text() == "new"


def test_a_callable_probe_marked_read_through_satisfies_the_data_rule(tmp_path):
    h = Hooks(tmp_path, probes=[Probe("reads", "callable", when="in_commit", proves="read_through", fn=lambda e: True),
                                Probe("up", "callable", fn=lambda e: True)])
    plan_for(tmp_path, h, data=True).prove()  # not refused
    h2 = Hooks(tmp_path, probes=[Probe("reads", "callable", when="in_commit", fn=lambda e: True)])
    with pytest.raises(GateNotPassed, match="relation_read"):
        plan_for(tmp_path, h2, data=True).prove()
