# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""A surface declares whether it can ask a human; it is never sniffed.

An approval policy answers the gate for a surface with nobody at a keyboard.
It decides from a declared allowlist, refuses everything else, says why, and
touches no input stream at all. The agent consults a policy only when one has
been set, so every existing caller keeps today's interactive prompt.
"""

from __future__ import annotations

import builtins
from unittest.mock import MagicMock

import pytest

from axiom.extensions.builtins.chat.approval_policy import (
    ApprovalDecision,
    ApprovalPolicy,
    NonInteractiveApprovalPolicy,
)
from axiom.extensions.builtins.chat.permissions import parse_approval_choice
from axiom.infra.orchestrator.actions import create_action


def _write_action(name: str = "write_file"):
    return create_action(name, {"file_path": "/tmp/x.txt", "content": "hi"})


class _ExplodingStdin:
    """Any read at all is a defect: a policy decides without asking."""

    def _boom(self, *_a, **_kw):
        raise AssertionError("the approval policy read stdin")

    read = readline = readlines = __iter__ = __next__ = _boom

    def isatty(self) -> bool:
        raise AssertionError("the approval policy sniffed for a terminal")


@pytest.fixture
def no_input(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make every route to a human raise instead of blocking."""

    def _boom(*_a, **_kw):
        raise AssertionError("something prompted a human")

    monkeypatch.setattr(builtins, "input", _boom)
    monkeypatch.setattr("sys.stdin", _ExplodingStdin())


class TestApprovalDecision:
    """The decision is the choice letter, with the reason still attached."""

    def test_compares_equal_to_the_bare_choice(self):
        assert ApprovalDecision("r", "because") == "r"

    def test_carries_the_reason(self):
        assert ApprovalDecision("r", "because").reason == "because"

    def test_exposes_the_choice(self):
        assert ApprovalDecision("a", "ok").choice == "a"

    def test_the_shared_parser_still_recognises_it(self):
        for choice in ("a", "A", "r", "D"):
            assert parse_approval_choice(ApprovalDecision(choice, "why")) == choice

    def test_repr_shows_the_reason(self):
        assert "why" in repr(ApprovalDecision("r", "why"))


class TestNonInteractivePolicy:
    """Refuse by default; widen only by name."""

    def test_is_an_approval_policy(self):
        assert isinstance(NonInteractiveApprovalPolicy(), ApprovalPolicy)

    def test_rejects_an_action_that_is_not_allowlisted(self, no_input):
        assert NonInteractiveApprovalPolicy().decide(_write_action()) == "r"

    def test_rejects_when_the_allowlist_names_other_tools(self, no_input):
        policy = NonInteractiveApprovalPolicy(allow=["doc_publish"])
        assert policy.decide(_write_action("write_file")) == "r"

    def test_refusal_names_the_tool_and_the_allowlist(self, no_input):
        decision = NonInteractiveApprovalPolicy().decide(_write_action())
        assert "write_file" in decision.reason
        assert "allowlist" in decision.reason.lower()

    def test_approves_an_allowlisted_action(self, no_input):
        policy = NonInteractiveApprovalPolicy(allow=["write_file"])
        assert policy.decide(_write_action()) == "a"

    def test_approval_names_the_tool(self, no_input):
        policy = NonInteractiveApprovalPolicy(allow=["write_file"])
        assert "write_file" in policy.decide(_write_action()).reason

    def test_approval_does_not_persist_across_sessions(self, no_input):
        """``a`` approves this one call; ``A`` would write a lasting override."""
        policy = NonInteractiveApprovalPolicy(allow=["write_file"])
        assert policy.decide(_write_action()).choice == "a"

    def test_refusal_does_not_persist_across_sessions(self, no_input):
        """``r`` refuses this one call; ``D`` would write a lasting denial."""
        assert NonInteractiveApprovalPolicy().decide(_write_action()).choice == "r"

    def test_empty_allowlist_is_the_default(self):
        assert NonInteractiveApprovalPolicy().allowed == frozenset()

    def test_allowlist_is_reported_by_name(self):
        policy = NonInteractiveApprovalPolicy(allow=["write_file", "doc_publish"])
        assert policy.allowed == frozenset({"write_file", "doc_publish"})

    def test_the_allowlist_cannot_be_widened_after_construction(self):
        policy = NonInteractiveApprovalPolicy(allow=["write_file"])
        with pytest.raises(AttributeError):
            policy.allowed.add("doc_publish")  # type: ignore[attr-defined]

    def test_decides_with_stdin_closed(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr("sys.stdin", None)
        assert NonInteractiveApprovalPolicy().decide(_write_action()) == "r"

    def test_writes_nothing_while_deciding(self, capsys, no_input):
        policy = NonInteractiveApprovalPolicy(allow=["doc_publish"])
        policy.decide(_write_action("write_file"))
        policy.decide(_write_action("doc_publish"))
        captured = capsys.readouterr()
        assert captured.out == ""
        assert captured.err == ""


def _agent():
    from axiom.extensions.builtins.chat.agent import ChatAgent
    from axiom.infra.bus import EventBus
    from axiom.infra.gateway import Gateway
    from axiom.infra.orchestrator.session import Session

    gw = MagicMock(spec=Gateway)
    gw.available = True
    gw.active_provider = MagicMock()
    gw.active_provider.name = "test"
    gw.active_provider.model = "test-model"
    return ChatAgent(gateway=gw, bus=EventBus(), session=Session())


def _response(tool_name: str = "write_file", **params):
    from axiom.infra.gateway import CompletionResponse, ToolUseBlock

    return CompletionResponse(
        text="",
        tool_use=[ToolUseBlock(tool_id="t1", name=tool_name, input=dict(params))],
        provider="test",
        success=True,
    )


class TestAgentWithAPolicy:
    """A declared policy is consulted instead of any prompt."""

    def test_constructor_accepts_a_policy(self):
        from axiom.extensions.builtins.chat.agent import ChatAgent
        from axiom.infra.bus import EventBus
        from axiom.infra.gateway import Gateway
        from axiom.infra.orchestrator.session import Session

        policy = NonInteractiveApprovalPolicy()
        agent = ChatAgent(
            gateway=MagicMock(spec=Gateway),
            bus=EventBus(),
            session=Session(),
            approval_policy=policy,
        )
        assert agent.approval_policy is policy

    def test_setter_installs_a_policy(self):
        policy = NonInteractiveApprovalPolicy()
        agent = _agent()
        agent.set_approval_policy(policy)
        assert agent.approval_policy is policy

    def test_policy_refuses_and_nothing_prompts(self, no_input, tmp_path):
        agent = _agent()
        agent.set_approval_policy(NonInteractiveApprovalPolicy())
        target = tmp_path / "out.txt"
        results = agent._process_tool_calls(
            _response("write_file", file_path=str(target), content="hello")
        )
        assert len(results) == 1
        assert "error" in results[0][2]
        assert not target.exists()

    def test_refusal_reason_reaches_the_caller(self, no_input, tmp_path):
        agent = _agent()
        agent.set_approval_policy(NonInteractiveApprovalPolicy())
        results = agent._process_tool_calls(
            _response("write_file", file_path=str(tmp_path / "out.txt"), content="x")
        )
        error = results[0][2]["error"]
        assert "write_file" in error
        assert "allowlist" in error.lower()
        assert error not in ("r", "Rejected by user")

    def test_allowlisted_tool_runs(self, no_input, tmp_path):
        agent = _agent()
        agent.set_approval_policy(NonInteractiveApprovalPolicy(allow=["write_file"]))
        target = tmp_path / "out.txt"
        results = agent._process_tool_calls(
            _response("write_file", file_path=str(target), content="hello world")
        )
        assert "error" not in results[0][2]
        assert target.read_text() == "hello world"

    def test_policy_wins_over_a_render_provider_prompt(self, no_input, tmp_path):
        agent = _agent()
        render = MagicMock()
        render.render_approval_prompt.return_value = "a"
        agent.set_render_provider(render)
        agent.set_approval_policy(NonInteractiveApprovalPolicy())
        target = tmp_path / "out.txt"
        agent._process_tool_calls(_response("write_file", file_path=str(target), content="hello"))
        render.render_approval_prompt.assert_not_called()
        assert not target.exists()

    def test_read_only_tools_never_reach_the_policy(self, no_input):
        """The gate auto-approves reads, so a policy only ever sees writes."""
        policy = MagicMock(spec=ApprovalPolicy)
        agent = _agent()
        agent.set_approval_policy(policy)
        agent._process_tool_calls(_response("list_files", path="."))
        policy.decide.assert_not_called()

    def test_a_persisted_deny_still_wins_over_the_policy(self, no_input, tmp_path):
        policy = MagicMock(spec=ApprovalPolicy)
        agent = _agent()
        agent.permissions.set("write_file", "deny")
        agent.set_approval_policy(policy)
        results = agent._process_tool_calls(
            _response("write_file", file_path=str(tmp_path / "out.txt"), content="x")
        )
        policy.decide.assert_not_called()
        assert "error" in results[0][2]


class TestAgentWithoutAPolicy:
    """Leaving the policy unset changes nothing about today's behaviour."""

    def test_policy_is_unset_by_default(self):
        assert _agent().approval_policy is None

    def test_the_render_provider_is_still_prompted(self, tmp_path):
        agent = _agent()
        render = MagicMock()
        render.render_approval_prompt.return_value = "a"
        agent.set_render_provider(render)
        target = tmp_path / "out.txt"
        agent._process_tool_calls(_response("write_file", file_path=str(target), content="hello"))
        render.render_approval_prompt.assert_called_once()
        assert target.read_text() == "hello"

    def test_the_bare_prompt_is_still_the_fallback(self, monkeypatch, tmp_path):
        asked: list[str] = []

        def _answer(prompt: str = "") -> str:
            asked.append(prompt)
            return "a"

        monkeypatch.setattr(builtins, "input", _answer)
        agent = _agent()
        target = tmp_path / "out.txt"
        agent._process_tool_calls(_response("write_file", file_path=str(target), content="hello"))
        assert asked, "the interactive prompt was not reached"
        assert target.read_text() == "hello"

    def test_a_bare_reject_still_reads_as_a_user_rejection(self, monkeypatch, tmp_path):
        monkeypatch.setattr(builtins, "input", lambda _p="": "r")
        agent = _agent()
        results = agent._process_tool_calls(
            _response("write_file", file_path=str(tmp_path / "out.txt"), content="x")
        )
        assert results[0][2]["error"] == "Rejected by user"
