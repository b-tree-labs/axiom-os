# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""The human's side of the gate, and the surfaces it must not reach."""

import logging

import pytest

from axiom.infra.orchestrator import skills as approval_skills
from axiom.infra.orchestrator.actions import create_action
from axiom.infra.orchestrator.approval import ApprovalGate
from axiom.infra.orchestrator.approval_store import FileActionStore
from axiom.infra.skills import SkillContext, SkillRegistry


@pytest.fixture
def ctx(tmp_path):
    registry = SkillRegistry()
    approval_skills.register_all(registry)
    return SkillContext(
        registry=registry,
        state_dir=tmp_path,
        logger=logging.getLogger("test"),
        actor="@ben:ut",
    )


def _hold_one(ctx):
    gate = ApprovalGate(FileActionStore(ctx.state_dir / "orchestrator" / "approvals.json"))
    return gate.submit(create_action("doc_publish", {"source": "prd_foo.md"}))


class TestAnAgentCannotApproveItself:
    """The load-bearing test in this file.

    A gate whose approve verb is reachable from an agent tool is not a gate:
    the agent proposes a write, the gate holds it, and the agent approves it.
    The guarded failure is a one-word edit that looks like an improvement.
    """

    @pytest.mark.parametrize("name", ["approval.approve", "approval.reject"])
    @pytest.mark.parametrize("forbidden", ["agent_tool", "mcp"])
    def test_a_decision_verb_is_never_projected_to_an_agent(self, ctx, name, forbidden):
        assert forbidden not in ctx.registry.spec(name).surfaces

    def test_asking_what_is_waiting_is_fine_from_anywhere(self, ctx):
        """An agent that can see its blocked queue says so instead of stalling."""
        assert set(ctx.registry.spec("approval.pending").surfaces) == {
            "cli",
            "mcp",
            "agent_tool",
            "skill_md",
        }

    def test_the_decision_verbs_declare_side_effects(self, ctx):
        for name in ("approval.approve", "approval.reject"):
            assert ctx.registry.spec(name).side_effects is True
        assert ctx.registry.spec("approval.pending").side_effects is False


class TestPending:
    def test_it_reports_what_is_held(self, ctx):
        action = _hold_one(ctx)
        result = approval_skills.pending({}, ctx)
        assert result.ok
        assert result.value["count"] == 1
        assert result.value["pending"][0]["action_id"] == action.action_id

    def test_an_empty_queue_is_not_an_error(self, ctx):
        result = approval_skills.pending({}, ctx)
        assert result.ok and result.value["count"] == 0


class TestDeciding:
    def test_approval_records_who_decided(self, ctx):
        action = _hold_one(ctx)
        result = approval_skills.approve({"action_id": action.action_id}, ctx)
        assert result.ok
        assert result.value["decided_by"] == "@ben:ut"
        assert result.value["action"]["status"] == "approved"

    def test_an_answered_action_reports_the_standing_answer(self, ctx):
        """Two humans racing is not a mistake; the useful reply is who won."""
        action = _hold_one(ctx)
        approval_skills.approve({"action_id": action.action_id}, ctx)

        again = approval_skills.approve({"action_id": action.action_id}, ctx)
        assert not again.ok
        assert "already approved" in again.errors[0]
        assert "@ben:ut" in again.errors[0]

    def test_an_unknown_id_says_where_to_look(self, ctx):
        result = approval_skills.approve({"action_id": "nosuch"}, ctx)
        assert not result.ok
        assert "axi approve list" in result.errors[0]

    def test_action_id_is_required(self, ctx):
        assert not approval_skills.approve({}, ctx).ok

    def test_rejection_carries_the_reason(self, ctx):
        action = _hold_one(ctx)
        result = approval_skills.reject(
            {"action_id": action.action_id, "reason": "wrong source"}, ctx
        )
        assert result.ok and result.value["action"]["status"] == "rejected"


class TestAttribution:
    def test_a_decision_is_never_recorded_unattributed(self, tmp_path):
        """An unknown decider is recorded as unknown, not omitted."""
        registry = SkillRegistry()
        approval_skills.register_all(registry)
        anonymous = SkillContext(
            registry=registry, state_dir=tmp_path, logger=logging.getLogger("t")
        )
        action = _hold_one(anonymous)
        result = approval_skills.approve({"action_id": action.action_id}, anonymous)
        assert result.value["decided_by"], "blank attribution is the bug this prevents"


class TestTheSkillIsDurableRegardlessOfTheLibraryDefault:
    def test_a_verb_typed_at_a_terminal_sees_another_process_queue(self, ctx):
        """The CLI is by definition not the process that proposed the action."""
        action = _hold_one(ctx)
        assert approval_skills.pending({}, ctx).value["pending"][0]["action_id"] == (
            action.action_id
        )
