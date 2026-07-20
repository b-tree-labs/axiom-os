# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tests for the approval gate — safe tools auto-approved, writes require confirmation."""

from unittest.mock import MagicMock

import pytest

from axiom.infra.orchestrator.actions import (
    ActionCategory,
    ActionStatus,
    create_action,
)
from axiom.infra.orchestrator.approval import ApprovalGate


class TestApprovalGateClassification:
    """Test that the approval gate correctly classifies read vs write tools."""

    SAFE_TOOLS = [
        "query_docs",
        "list_providers",
        "list_files",
        "read_file",
        "search_docs",
        "signal_status",
        "doc_check_links",
        "doc_diff",
    ]

    WRITE_TOOLS = [
        "write_file",
        "doc_generate",
        "doc_publish",
        "signal_ingest",
        "write_inbox_note",
    ]

    @pytest.fixture
    def gate(self):
        return ApprovalGate()

    @pytest.mark.parametrize("tool_name", SAFE_TOOLS)
    def test_safe_tools_auto_approved(self, gate, tool_name):
        """Read-only tools should be auto-approved without user interaction."""
        action = create_action(tool_name, {})
        assert action.category == ActionCategory.READ

        gate.submit(action)
        assert action.status == ActionStatus.APPROVED

    @pytest.mark.parametrize("tool_name", WRITE_TOOLS)
    def test_write_tools_require_approval(self, gate, tool_name):
        """Write tools should remain PENDING until explicitly approved."""
        action = create_action(tool_name, {"source": "test.md"})
        assert action.category == ActionCategory.WRITE

        gate.submit(action)
        assert action.status == ActionStatus.PENDING

    def test_unknown_tool_defaults_to_write(self, gate):
        """Tools not in the registry default to WRITE (safe default)."""
        action = create_action("unknown_dangerous_tool", {})
        assert action.category == ActionCategory.WRITE

        gate.submit(action)
        assert action.status == ActionStatus.PENDING

    def test_approve_pending_action(self, gate):
        action = create_action("write_file", {"file_path": "test.txt", "content": "hi"})
        gate.submit(action)
        assert action.status == ActionStatus.PENDING

        gate.approve(action.action_id)
        assert action.status == ActionStatus.APPROVED

    def test_reject_pending_action(self, gate):
        action = create_action("doc_publish", {"source": "test.md"})
        gate.submit(action)

        gate.reject(action.action_id, "User declined")
        assert action.status == ActionStatus.REJECTED
        assert action.error == "User declined"

    def test_pending_list(self, gate):
        a1 = create_action("list_files", {})
        a2 = create_action("write_file", {"file_path": "x", "content": "y"})
        a3 = create_action("doc_publish", {"source": "z"})
        gate.submit(a1)
        gate.submit(a2)
        gate.submit(a3)

        pending = gate.pending()
        assert len(pending) == 2
        assert a1 not in pending  # auto-approved
        assert a2 in pending
        assert a3 in pending


class TestWriteFileApprovalInAgent:
    """Test that write_file goes through approval in the agent's tool loop."""

    def test_write_file_rejected_by_user(self):
        """When user rejects write_file, the tool should not execute."""
        from axiom.extensions.builtins.chat.agent import ChatAgent
        from axiom.infra.bus import EventBus
        from axiom.infra.gateway import CompletionResponse, Gateway, ToolUseBlock
        from axiom.infra.orchestrator.session import Session

        gw = MagicMock(spec=Gateway)
        gw.available = True
        gw.active_provider = MagicMock()
        gw.active_provider.name = "test"
        gw.active_provider.model = "test-model"

        agent = ChatAgent(gateway=gw, bus=EventBus(), session=Session())

        # Mock render provider to simulate user rejecting
        render = MagicMock()
        render.render_approval_prompt.return_value = "n"  # User says no
        agent.set_render_provider(render)

        response = CompletionResponse(
            text="Let me write that.",
            tool_use=[
                ToolUseBlock(
                    tool_id="t1",
                    name="write_file",
                    input={"file_path": "/tmp/test.txt", "content": "hello"},
                )
            ],
            provider="test",
            success=True,
        )

        results = agent._process_tool_calls(response)
        assert len(results) == 1
        _tid, _name, result = results[0]
        assert "error" in result
        assert "Rejected" in result["error"]

    def test_write_file_approved_by_user(self, tmp_path):
        """When user approves write_file, the file should be written."""
        from axiom.extensions.builtins.chat.agent import ChatAgent
        from axiom.infra.bus import EventBus
        from axiom.infra.gateway import CompletionResponse, Gateway, ToolUseBlock
        from axiom.infra.orchestrator.session import Session

        gw = MagicMock(spec=Gateway)
        gw.available = True
        gw.active_provider = MagicMock()
        gw.active_provider.name = "test"
        gw.active_provider.model = "test-model"

        agent = ChatAgent(gateway=gw, bus=EventBus(), session=Session())

        render = MagicMock()
        render.render_approval_prompt.return_value = "a"  # User approves
        agent.set_render_provider(render)

        target = tmp_path / "output.txt"
        response = CompletionResponse(
            text="Writing now.",
            tool_use=[
                ToolUseBlock(
                    tool_id="t1",
                    name="write_file",
                    input={"file_path": str(target), "content": "hello world"},
                )
            ],
            provider="test",
            success=True,
        )

        results = agent._process_tool_calls(response)
        assert len(results) == 1
        _tid, _name, result = results[0]
        assert "error" not in result
        assert target.read_text() == "hello world"


class TestAlwaysAllowlist:
    """Test per-session always-allowlist (A/Always approval)."""

    def _make_agent(self):
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

    def _make_response(self, tool_name, tool_id="t1", file_path="/tmp/t.txt"):
        from axiom.infra.gateway import CompletionResponse, ToolUseBlock

        return CompletionResponse(
            text="",
            tool_use=[
                ToolUseBlock(
                    tool_id=tool_id,
                    name=tool_name,
                    input={"file_path": file_path, "content": "x"},
                )
            ],
            provider="test",
            success=True,
        )

    def test_always_approval_persists_for_session(self, tmp_path):
        """Approving with 'A' allowlists the tool — second call skips the prompt."""
        agent = self._make_agent()
        render = MagicMock()
        # First call returns 'A' (always), second call should NOT be prompted
        render.render_approval_prompt.return_value = "A"
        agent.set_render_provider(render)

        f1 = tmp_path / "a.txt"
        f2 = tmp_path / "b.txt"

        agent._process_tool_calls(self._make_response("write_file", "t1", str(f1)))
        # Allowlist should now contain write_file; second call should auto-approve
        render.render_approval_prompt.return_value = "should_not_be_called"
        agent._process_tool_calls(self._make_response("write_file", "t2", str(f2)))

        # Prompt was only called once (second call was auto-approved)
        assert render.render_approval_prompt.call_count == 1

    def test_always_allowlist_is_per_tool_not_global(self, tmp_path):
        """Allowlisting write_file does not auto-approve doc_publish."""
        agent = self._make_agent()
        render = MagicMock()
        render.render_approval_prompt.return_value = "A"
        agent.set_render_provider(render)

        f1 = tmp_path / "a.txt"
        agent._process_tool_calls(self._make_response("write_file", "t1", str(f1)))

        # write_file is now allowlisted; doc_publish should still prompt
        render.render_approval_prompt.return_value = "r"
        agent._process_tool_calls(self._make_response("doc_publish", "t2", str(f1)))

        # Total prompts: 1 for write_file (A) + 1 for doc_publish = 2
        assert render.render_approval_prompt.call_count == 2
