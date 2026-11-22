# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""A hook demanding approval now holds the work instead of discarding it.

This branch used to end the story: it told the caller to find an interactive
surface, and threw the call away. On a scheduled agent or an MCP client that is
a refusal with extra steps, and the pause it described had nowhere to live.
"""

import logging

import pytest

from axiom.infra.hooks import ApprovalRequired
from axiom.infra.orchestrator.actions import ActionStatus
from axiom.infra.orchestrator.approval import ApprovalGate
from axiom.infra.orchestrator.approval_store import FileActionStore
from axiom.infra.orchestrator.runner import ActionRunner
from axiom.infra.skill_dispatch import RUNNER_SURFACE, invoke_capability
from axiom.infra.skills import SkillContext, SkillRegistry, SkillResult, SkillSpec


@pytest.fixture
def world(tmp_path, monkeypatch):
    registry = SkillRegistry()
    ran: list[dict] = []

    def publish(params, ctx):
        ran.append(dict(params))
        (tmp_path / "published.txt").write_text(params.get("body", ""))
        return SkillResult(ok=True, value={"ok": True})

    registry.register_skill(SkillSpec(name="doc.publish", fn=publish, idempotent=True))
    ctx = SkillContext(
        registry=registry,
        state_dir=tmp_path,
        logger=logging.getLogger("t"),
        actor="@ben:ut",
    )
    gate = ApprovalGate(FileActionStore(tmp_path / "orchestrator" / "approvals.json"))
    return {"registry": registry, "ctx": ctx, "gate": gate, "tmp": tmp_path, "ran": ran}


def _demand_approval(monkeypatch, reason="site rule: publishing needs a signoff"):
    def demanding(*args, **kwargs):
        raise ApprovalRequired(reason, hook_source="site")

    monkeypatch.setattr("axiom.infra.tool_gateway.dispatch_tool", demanding)


class TestTheWorkIsHeldNotDiscarded:
    def test_the_call_lands_on_the_durable_queue(self, world, monkeypatch):
        _demand_approval(monkeypatch)

        result = invoke_capability(
            world["registry"],
            "doc.publish",
            {"body": "hello"},
            world["ctx"],
            surface="mcp",
        )

        assert not result.ok, "the work did not happen"
        held = result.value["held_action_id"]
        assert world["gate"].get(held).status == ActionStatus.PENDING

    def test_the_held_action_keeps_the_params_so_it_can_actually_run_later(
        self, world, monkeypatch
    ):
        _demand_approval(monkeypatch)
        result = invoke_capability(
            world["registry"], "doc.publish", {"body": "hello"}, world["ctx"], surface="mcp"
        )

        action = world["gate"].get(result.value["held_action_id"])
        assert action.name == "doc.publish"
        assert action.params == {"body": "hello"}

    def test_the_queue_records_why_a_human_was_asked(self, world, monkeypatch):
        """A queue showing what waits but not why gets approved reflexively."""
        _demand_approval(monkeypatch, reason="site rule: publishing needs a signoff")
        result = invoke_capability(
            world["registry"], "doc.publish", {"body": "x"}, world["ctx"], surface="mcp"
        )

        assert "needs a signoff" in world["gate"].get(result.value["held_action_id"]).reason

    def test_the_error_tells_the_caller_exactly_what_to_type(self, world, monkeypatch):
        _demand_approval(monkeypatch)
        result = invoke_capability(
            world["registry"], "doc.publish", {"body": "x"}, world["ctx"], surface="mcp"
        )

        assert "axi approve ok" in result.errors[0]
        assert result.value["held_action_id"] in result.errors[0]

    def test_nothing_ran(self, world, monkeypatch):
        _demand_approval(monkeypatch)
        invoke_capability(
            world["registry"], "doc.publish", {"body": "x"}, world["ctx"], surface="mcp"
        )

        assert world["ran"] == []
        assert not (world["tmp"] / "published.txt").exists()


class TestTheFullLoop:
    def test_held_then_approved_then_run(self, world, monkeypatch):
        """The whole point, end to end.

        A hook refuses the call, a human answers later, and the work happens.
        Before this, the call was gone and the human was never asked.
        """
        _demand_approval(monkeypatch)
        result = invoke_capability(
            world["registry"],
            "doc.publish",
            {"body": "landed"},
            world["ctx"],
            surface="mcp",
        )
        held = result.value["held_action_id"]

        # The hook stops demanding — a site rule changed, or the signoff is the
        # approval itself. The human answers, and a runner picks it up.
        monkeypatch.undo()
        world["gate"].approve(held, decided_by="@ben:ut")
        runner = ActionRunner(world["gate"], world["registry"], world["ctx"])

        assert runner.run_approved().completed == [held]
        assert (world["tmp"] / "published.txt").read_text() == "landed"


class TestNoInfiniteQueue:
    """The runner must never mint a fresh pending action on every pass."""

    def test_the_runner_does_not_re_queue_what_it_is_re_running(self, world, monkeypatch):
        _demand_approval(monkeypatch)
        held = invoke_capability(
            world["registry"], "doc.publish", {"body": "x"}, world["ctx"], surface="mcp"
        ).value["held_action_id"]
        world["gate"].approve(held, decided_by="@ben:ut")

        # The hook is STILL demanding approval when the runner re-invokes.
        runner = ActionRunner(world["gate"], world["registry"], world["ctx"])
        runner.run_approved()
        runner.run_approved()

        assert len(world["gate"].all_actions()) == 1, (
            "a second held action means every pass queues a new one, forever"
        )

    def test_the_runner_passes_the_flag_that_prevents_it(self, world, monkeypatch):
        """Pinned directly, so the guard cannot be removed by accident."""
        _demand_approval(monkeypatch)

        result = invoke_capability(
            world["registry"],
            "doc.publish",
            {"body": "x"},
            world["ctx"],
            surface=RUNNER_SURFACE,
            hold_on_approval=False,
        )

        assert result.value is None
        assert "cannot prompt" in result.errors[0]
        assert world["gate"].all_actions() == []


class TestAnUnwritableQueueIsNotSilent:
    def test_it_says_nothing_is_waiting_rather_than_inventing_an_id(
        self, world, monkeypatch
    ):
        """Reporting a held id for an unrecorded action is the worst outcome.

        A person would wait on a queue entry that does not exist.
        """
        _demand_approval(monkeypatch)
        monkeypatch.setattr(
            "axiom.infra.orchestrator.approval.ApprovalGate.submit",
            lambda self, action: (_ for _ in ()).throw(OSError("read-only fs")),
        )

        result = invoke_capability(
            world["registry"], "doc.publish", {"body": "x"}, world["ctx"], surface="mcp"
        )

        assert not result.ok
        assert result.value is None
        assert "could not be queued" in result.errors[0]
        assert "Nothing is waiting" in result.errors[0]
