# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""The silent render provider: a chat turn with no terminal attached.

A serving worker has no console to paint and no operator to ask. This provider
implements the full render contract, writes nothing to stdout or stderr, still
drains the chunk iterator so the agent can reconstruct the turn, and answers
approvals from its declared policy instead of prompting.
"""

from __future__ import annotations

import builtins
from unittest.mock import MagicMock

import pytest

from axiom.extensions.builtins.chat.approval_policy import (
    ApprovalPolicy,
    NonInteractiveApprovalPolicy,
)
from axiom.extensions.builtins.chat.providers.base import RenderProvider
from axiom.extensions.builtins.chat.providers.null_render import NullRenderProvider
from axiom.infra.gateway import StreamChunk
from axiom.infra.orchestrator.actions import ActionStatus, create_action


def _action():
    return create_action("write_file", {"file_path": "/tmp/x.txt", "content": "hi"})


def _chunks():
    return [
        StreamChunk(type="thinking_delta", text="pondering"),
        StreamChunk(type="text", text="Hello, "),
        StreamChunk(type="usage", input_tokens=3, output_tokens=4),
        StreamChunk(type="text", text="world"),
        StreamChunk(type="tool_use_start", tool_name="write_file", tool_id="t1"),
        StreamChunk(type="tool_input_delta", tool_id="t1", tool_input_json="{}"),
        StreamChunk(type="tool_use_end", tool_id="t1", tool_input_json="{}"),
        StreamChunk(type="done"),
    ]


@pytest.fixture
def no_input(monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom(*_a, **_kw):
        raise AssertionError("the null provider prompted a human")

    monkeypatch.setattr(builtins, "input", _boom)


def _exercise_every_render_method(provider: NullRenderProvider) -> None:
    """Call every method on the render contract once."""
    provider.stream_text(iter(_chunks()))
    provider.render_welcome(gateway=MagicMock(), show_banner=True, workspace_context="ws")
    provider.render_tool_start("write_file", {"file_path": "/tmp/x.txt"})
    provider.render_tool_result("write_file", {"ok": True}, 0.5)
    provider.render_approval_prompt(_action())
    completed = _action()
    completed.complete({"output": "/tmp/x.txt"})
    provider.render_action_result(completed)
    provider.render_status("test-model", 10, 20, 0.01)
    provider.render_thinking("reasoning", collapsed=False)
    provider.render_message("assistant", "hello")
    provider.render_session_list([{"id": "s1", "title": "one"}])


class TestContract:
    def test_is_a_render_provider(self):
        assert isinstance(NullRenderProvider(), RenderProvider)

    def test_implements_every_abstract_method(self):
        assert not getattr(NullRenderProvider, "__abstractmethods__", frozenset())

    def test_every_method_named_by_the_abc_is_exercised_here(self):
        """The silence test below must cover the whole contract, not part of it."""
        import inspect

        abstract = {
            name
            for name in vars(RenderProvider)
            if getattr(getattr(RenderProvider, name), "__isabstractmethod__", False)
        }
        body = inspect.getsource(_exercise_every_render_method)
        missing = {name for name in abstract if f".{name}(" not in body}
        assert not missing, f"render methods never exercised: {sorted(missing)}"


class TestSilence:
    def test_writes_nothing_on_any_render_method(self, capsys, no_input):
        _exercise_every_render_method(NullRenderProvider())
        captured = capsys.readouterr()
        assert captured.out == ""
        assert captured.err == ""

    def test_writes_nothing_when_approval_is_granted(self, capsys, no_input):
        provider = NullRenderProvider(NonInteractiveApprovalPolicy(allow=["write_file"]))
        provider.render_approval_prompt(_action())
        captured = capsys.readouterr()
        assert captured.out == ""
        assert captured.err == ""

    def test_writes_nothing_for_a_rejected_action(self, capsys):
        action = _action()
        action.reject("no")
        NullRenderProvider().render_action_result(action)
        assert action.status == ActionStatus.REJECTED
        captured = capsys.readouterr()
        assert captured.out == ""
        assert captured.err == ""


class TestStreamText:
    def test_returns_the_accumulated_text(self):
        assert NullRenderProvider().stream_text(iter(_chunks())) == "Hello, world"

    def test_returns_empty_text_for_a_toolonly_stream(self):
        chunks = [
            StreamChunk(type="tool_use_start", tool_name="write_file", tool_id="t1"),
            StreamChunk(type="done"),
        ]
        assert NullRenderProvider().stream_text(iter(chunks)) == ""

    def test_consumes_the_whole_iterator(self):
        """The agent rebuilds the turn from what the renderer pulled."""
        seen: list[str] = []

        def _gen():
            for chunk in _chunks():
                seen.append(chunk.type)
                yield chunk

        NullRenderProvider().stream_text(_gen())
        assert seen == [c.type for c in _chunks()]

    def test_an_agent_turn_keeps_its_tool_calls(self):
        """Draining the stream is what lets the tee reconstruct tool blocks."""
        from axiom.extensions.builtins.chat.agent import ChatAgent
        from axiom.infra.bus import EventBus
        from axiom.infra.gateway import Gateway
        from axiom.infra.orchestrator.session import Session

        gw = MagicMock(spec=Gateway)
        gw.stream_with_tools.return_value = iter(
            [
                StreamChunk(type="text", text="writing"),
                StreamChunk(type="tool_use_start", tool_name="write_file", tool_id="t1"),
                StreamChunk(type="tool_use_end", tool_id="t1", tool_input_json='{"a": 1}'),
                StreamChunk(type="done"),
            ]
        )
        agent = ChatAgent(gateway=gw, bus=EventBus(), session=Session())
        agent.set_render_provider(NullRenderProvider())
        response = agent._streaming_turn(messages=[], system="", tools=[])
        assert response.text == "writing"
        assert [b.name for b in response.tool_use] == ["write_file"]


class TestApproval:
    def test_default_policy_refuses(self, no_input):
        assert NullRenderProvider().render_approval_prompt(_action()) == "r"

    def test_default_refusal_says_why(self, no_input):
        decision = NullRenderProvider().render_approval_prompt(_action())
        assert "write_file" in decision.reason

    def test_an_allowlisted_action_is_approved(self, no_input):
        provider = NullRenderProvider(NonInteractiveApprovalPolicy(allow=["write_file"]))
        assert provider.render_approval_prompt(_action()) == "a"

    def test_the_declared_policy_is_the_one_consulted(self, no_input):
        policy = MagicMock(spec=ApprovalPolicy)
        policy.decide.return_value = "a"
        provider = NullRenderProvider(policy)
        assert provider.render_approval_prompt(_action()) == "a"
        policy.decide.assert_called_once()

    def test_the_policy_is_readable(self):
        policy = NonInteractiveApprovalPolicy(allow=["doc_publish"])
        assert NullRenderProvider(policy).approval_policy is policy

    def test_the_default_policy_allows_nothing(self):
        assert NullRenderProvider().approval_policy.allowed == frozenset()
