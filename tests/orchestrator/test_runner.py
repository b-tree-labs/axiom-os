# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""What happens after the human says yes."""

import logging

import pytest

from axiom.infra.orchestrator.actions import Action, ActionCategory, ActionStatus
from axiom.infra.orchestrator.approval import ApprovalGate
from axiom.infra.orchestrator.approval_store import FileActionStore
from axiom.infra.orchestrator.runner import ActionRunner
from axiom.infra.skills import SkillContext, SkillRegistry, SkillResult, SkillSpec


@pytest.fixture
def world(tmp_path):
    """A gate, a registry with one real side effect, and a runner over both."""
    registry = SkillRegistry()
    calls: list[dict] = []

    def write_report(params, ctx):
        calls.append(dict(params))
        target = tmp_path / params["name"]
        target.write_text(params["body"])
        return SkillResult(ok=True, value={"wrote": str(target)})

    def always_fails(params, ctx):
        return SkillResult(ok=False, errors=["the model refused"])

    def explodes(params, ctx):
        raise RuntimeError("boom")

    registry.register_skill(
        SkillSpec(name="demo.write_report", fn=write_report, idempotent=True)
    )
    registry.register_skill(
        SkillSpec(name="demo.fails", fn=always_fails, idempotent=False)
    )
    registry.register_skill(SkillSpec(name="demo.explodes", fn=explodes, idempotent=False))

    gate = ApprovalGate(FileActionStore(tmp_path / "approvals.json"))
    ctx = SkillContext(
        registry=registry, state_dir=tmp_path, logger=logging.getLogger("t"), actor="@ben:ut"
    )
    runner = ActionRunner(gate, registry, ctx)
    return {
        "gate": gate,
        "runner": runner,
        "registry": registry,
        "ctx": ctx,
        "tmp": tmp_path,
        "calls": calls,
    }


def _propose(gate, name, params=None):
    return gate.submit(Action(name=name, params=params or {}, category=ActionCategory.WRITE))


class TestApprovalActuallyDoesSomething:
    """The gap this closes: an approved action that never ran.

    The record said approved, the operator believed it happened, and it did
    not. That is worse than never asking.
    """

    def test_an_approved_action_runs_and_has_its_real_effect(self, world):
        action = _propose(
            world["gate"], "demo.write_report", {"name": "report.md", "body": "# Ready"}
        )
        world["gate"].approve(action.action_id, decided_by="@ben:ut")

        report = world["runner"].run_approved()

        assert report.completed == [action.action_id]
        assert (world["tmp"] / "report.md").read_text() == "# Ready"

    def test_a_pending_action_does_not_run(self, world):
        """The whole point. Nobody said yes."""
        _propose(world["gate"], "demo.write_report", {"name": "no.md", "body": "x"})

        assert not world["runner"].run_approved()
        assert not (world["tmp"] / "no.md").exists()

    def test_a_rejected_action_does_not_run(self, world):
        action = _propose(world["gate"], "demo.write_report", {"name": "no.md", "body": "x"})
        world["gate"].reject(action.action_id, "wrong revision", decided_by="@ben:ut")

        assert not world["runner"].run_approved()
        assert not (world["tmp"] / "no.md").exists()

    def test_running_twice_does_not_run_the_action_twice(self, world):
        action = _propose(
            world["gate"], "demo.write_report", {"name": "once.md", "body": "x"}
        )
        world["gate"].approve(action.action_id, decided_by="@ben:ut")

        world["runner"].run_approved()
        second = world["runner"].run_approved()

        assert second.completed == []
        assert len(world["calls"]) == 1
        assert world["gate"].get(action.action_id).status == ActionStatus.COMPLETED


class TestFailure:
    def test_a_skill_that_reports_failure_marks_the_action_failed(self, world):
        action = _propose(world["gate"], "demo.fails")
        world["gate"].approve(action.action_id, decided_by="@ben:ut")

        report = world["runner"].run_approved()

        assert report.failed == [action.action_id]
        settled = world["gate"].get(action.action_id)
        assert settled.status == ActionStatus.FAILED
        assert "the model refused" in settled.error

    def test_a_raising_skill_fails_the_action_rather_than_the_pass(self, world):
        boom = _propose(world["gate"], "demo.explodes")
        fine = _propose(
            world["gate"], "demo.write_report", {"name": "still.md", "body": "ran"}
        )
        for a in (boom, fine):
            world["gate"].approve(a.action_id, decided_by="@ben:ut")

        report = world["runner"].run_approved()

        assert boom.action_id in report.failed
        assert fine.action_id in report.completed, "one bad action must not stop the pass"
        assert (world["tmp"] / "still.md").exists()


class TestUnroutable:
    def test_an_unknown_skill_leaves_the_decision_intact(self, world):
        """A typo in our wiring must not burn the human's answer."""
        action = _propose(world["gate"], "demo.not_registered")
        world["gate"].approve(action.action_id, decided_by="@ben:ut")

        report = world["runner"].run_approved()

        assert report.unroutable == [action.action_id]
        assert world["gate"].get(action.action_id).status == ActionStatus.APPROVED

    def test_checking_routability_does_not_execute_anything(self, world):
        """Regression: the first version probed by invoking the skill.

        Asking "does this exist" by calling it would run every approved action
        an extra time, including the ones it then declined to claim.
        """
        action = _propose(
            world["gate"], "demo.write_report", {"name": "probe.md", "body": "x"}
        )
        world["gate"].approve(action.action_id, decided_by="@ben:ut")

        world["runner"].run_approved()

        assert len(world["calls"]) == 1, "existence check must not be an invocation"


class TestCrashSafety:
    def test_a_claimed_action_says_running_not_approved(self, world):
        """A crash mid-flight must not read as 'never started'."""
        action = _propose(
            world["gate"], "demo.write_report", {"name": "r.md", "body": "x"}
        )
        world["gate"].approve(action.action_id, decided_by="@ben:ut")

        seen = {}

        def observe(params, ctx):
            seen["status"] = world["gate"].get(action.action_id).status
            return SkillResult(ok=True)

        world["registry"]._skills["demo.write_report"] = observe
        world["runner"].run_approved()

        assert seen["status"] == ActionStatus.RUNNING

    def test_a_stranded_non_idempotent_action_is_not_silently_rerun(self, world):
        """One approval becoming two writes is the failure being prevented."""
        action = _propose(world["gate"], "demo.fails")
        world["gate"].approve(action.action_id, decided_by="@ben:ut")
        stuck = world["gate"].get(action.action_id)
        stuck.status = ActionStatus.RUNNING
        world["gate"].record(stuck)

        report = world["runner"].resume_stranded()

        assert report.stranded == [action.action_id]
        assert world["gate"].get(action.action_id).status == ActionStatus.RUNNING

    def test_a_stranded_idempotent_action_resumes(self, world):
        action = _propose(
            world["gate"], "demo.write_report", {"name": "resumed.md", "body": "back"}
        )
        world["gate"].approve(action.action_id, decided_by="@ben:ut")
        stuck = world["gate"].get(action.action_id)
        stuck.status = ActionStatus.RUNNING
        world["gate"].record(stuck)

        report = world["runner"].resume_stranded()

        assert report.completed == [action.action_id]
        assert (world["tmp"] / "resumed.md").read_text() == "back"

    def test_force_resumes_even_a_non_idempotent_one(self, world):
        action = _propose(world["gate"], "demo.fails")
        world["gate"].approve(action.action_id, decided_by="@ben:ut")
        stuck = world["gate"].get(action.action_id)
        stuck.status = ActionStatus.RUNNING
        world["gate"].record(stuck)

        report = world["runner"].resume_stranded(force=True)

        assert report.failed == [action.action_id]


class TestCrossProcess:
    def test_the_runner_need_not_be_the_process_that_asked(self, world, tmp_path):
        """The point of the durable store, exercised end to end."""
        path = tmp_path / "cross.json"
        proposer = ApprovalGate(FileActionStore(path))
        action = _propose(
            proposer, "demo.write_report", {"name": "cross.md", "body": "landed"}
        )

        ApprovalGate(FileActionStore(path)).approve(action.action_id, decided_by="@ben:ut")

        worker = ActionRunner(
            ApprovalGate(FileActionStore(path)), world["registry"], world["ctx"]
        )
        assert worker.run_approved().completed == [action.action_id]
        assert (tmp_path / "cross.md").read_text() == "landed"


class TestRoutedThroughTheChokepoint:
    """The runner goes through ``invoke_capability``, not the registry.

    Reaching for ``SkillRegistry.invoke`` skips the ``tool.pre_invoke`` hooks,
    the GUARD consult and the refusal ledger. The first version of this module
    did exactly that and ``tests/infra/test_skill_dispatch.py`` caught it.
    """

    def test_a_hook_denial_fails_the_action_rather_than_running_it(self, world, monkeypatch):
        from axiom.infra.hooks import HookDenied

        def deny(*args, **kwargs):
            raise HookDenied("site rule forbids it", hook_source="test")

        monkeypatch.setattr("axiom.infra.tool_gateway.dispatch_tool", deny)

        action = _propose(
            world["gate"], "demo.write_report", {"name": "denied.md", "body": "x"}
        )
        world["gate"].approve(action.action_id, decided_by="@ben:ut")

        report = world["runner"].run_approved()

        assert report.failed == [action.action_id]
        assert not (world["tmp"] / "denied.md").exists(), "a denied action must not run"
        assert "denied by hook" in world["gate"].get(action.action_id).error

    def test_a_hook_demanding_approval_for_an_already_approved_action_is_visible(
        self, world, monkeypatch
    ):
        """Two approval mechanisms disagreeing must not be silent.

        A human approved this through the gate. A ``tool.pre_invoke`` hook then
        raises ``ApprovalRequired`` anyway, and ``invoke_capability`` turns that
        into "the runner surface cannot prompt for approval".

        Recorded here as the *current* behaviour, not the desired one: the
        action fails with a message naming the conflict, rather than running on
        the human's decision or looking like a crash. Resolving which mechanism
        wins is a policy question, and it stays visible until somebody answers
        it.
        """
        from axiom.infra.hooks import ApprovalRequired

        def needs_approval(*args, **kwargs):
            raise ApprovalRequired("site rule wants a second signoff", hook_source="test")

        monkeypatch.setattr("axiom.infra.tool_gateway.dispatch_tool", needs_approval)

        action = _propose(
            world["gate"], "demo.write_report", {"name": "twice.md", "body": "x"}
        )
        world["gate"].approve(action.action_id, decided_by="@ben:ut")

        world["runner"].run_approved()

        error = world["gate"].get(action.action_id).error
        assert "approval required" in error
        assert "runner" in error, "the message names the surface that could not ask"
        assert not (world["tmp"] / "twice.md").exists()
