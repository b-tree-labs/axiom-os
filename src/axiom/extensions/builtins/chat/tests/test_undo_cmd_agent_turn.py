# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""A real agent turn, with a scripted model, checkpoints what its write_file call does and /undo reverts it."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

from axiom.extensions.builtins.chat import checkpoints
from axiom.extensions.builtins.chat.approval_policy import NonInteractiveApprovalPolicy
from axiom.extensions.builtins.chat.headless import HeadlessChat
from axiom.extensions.builtins.chat.tests._cmd_helpers import _jsonl_ledger, run  # noqa: F401
from axiom.infra.gateway import CompletionResponse, Gateway, ToolUseBlock


def _gateway(target):
    gw = MagicMock(spec=Gateway)  # the scripted MODEL; the agent loop, tool dispatch and ledger are real
    gw.available = True
    gw.active_provider = MagicMock()
    gw.active_provider.name = "test"
    gw.active_provider.model = "test-model"
    calls = []

    def respond(*_a, **_kw):
        calls.append(1)
        if len(calls) == 1:
            return CompletionResponse(
                text="",
                tool_use=[
                    ToolUseBlock(
                        tool_id="t1", name="write_file",
                        input={"file_path": str(target), "content": "by the agent"},
                    )
                ],
                provider="test", success=True, stop_reason="tool_use",
            )
        return CompletionResponse(text="Done.", provider="test", success=True)

    gw.complete_with_tools.side_effect = respond
    return gw


def test_a_turn_through_the_agent_loop_is_one_undoable_checkpointed_turn(tmp_path):
    target = tmp_path / "doc.txt"
    target.write_text("mine")
    chat = HeadlessChat(
        turn_deadline=30.0,
        gateway=_gateway(target),
        approval_policy=NonInteractiveApprovalPolicy(allow=["write_file"]),
    )
    scope = chat.new_scope()
    chat.turn("rewrite it", stream=False, scope=scope)
    assert target.read_text() == "by the agent"
    (turn,) = checkpoints.history(scope.session.session_id)
    assert turn.paths == [str(target)]
    out = run("/undo", SimpleNamespace(session=scope.session))
    assert target.read_text() == "mine" and "undid turn 1" in out
