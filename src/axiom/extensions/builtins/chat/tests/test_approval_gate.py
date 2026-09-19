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
        "discover_verbs",
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


class TestProjectedCategoryApproval:
    """The loop classifies from the tool table the model was offered (ADR-072).

    Built-ins, extension tools and projected capabilities alike carry the
    category the shared projector derived on their ``ToolDef``. The
    orchestrator registry is only the fallback for names outside the table,
    and an unknown name stays WRITE.
    """

    @pytest.fixture
    def registry(self):
        from axiom.infra.skills import SkillRegistry, SkillResult, SkillSpec

        seen: list[tuple[str, dict]] = []

        def _status(params, ctx):
            seen.append(("scan.status", dict(params)))
            return SkillResult(ok=True, value={"count": 3})

        def _draft(params, ctx):
            seen.append(("press.draft", dict(params)))
            return SkillResult(ok=True, value={"rendered": params.get("source")})

        r = SkillRegistry()
        r.register_skill(
            SkillSpec(name="scan.status", fn=_status, description="Status.", side_effects=False)
        )
        r.register_skill(
            SkillSpec(
                name="press.draft", fn=_draft, description="Draft.", inputs={"source": "Path"}
            )
        )
        r.seen = seen  # type: ignore[attr-defined]
        return r

    @pytest.fixture
    def scoped(self, monkeypatch, registry):
        """Expose both namespaces through the chat loop's capability scan."""
        from axiom.extensions.builtins.chat import tools

        monkeypatch.setattr(tools, "_skill_registry", lambda: registry)
        monkeypatch.setattr(tools, "_skill_namespaces", lambda: ["scan", "press"])

    @staticmethod
    def _agent(choice):
        """An agent whose operator answers ``choice`` to every approval prompt."""
        from axiom.extensions.builtins.chat.agent import ChatAgent
        from axiom.infra.bus import EventBus
        from axiom.infra.gateway import Gateway
        from axiom.infra.orchestrator.session import Session

        gw = MagicMock(spec=Gateway)
        gw.available = True
        gw.active_provider = MagicMock()
        gw.active_provider.name = "test"
        gw.active_provider.model = "test-model"
        agent = ChatAgent(gateway=gw, bus=EventBus(), session=Session())
        render = MagicMock()
        render.render_approval_prompt.return_value = choice
        agent.set_render_provider(render)
        return agent, render

    @staticmethod
    def _call(name, params=None):
        from axiom.infra.gateway import CompletionResponse, ToolUseBlock

        return CompletionResponse(
            text="",
            tool_use=[ToolUseBlock(tool_id="t1", name=name, input=params or {})],
            provider="test",
            success=True,
        )

    def test_read_only_capability_runs_without_a_prompt(self, scoped, registry):
        agent, render = self._agent("r")  # a prompt, had it fired, would reject
        [(_tid, _name, result)] = agent._process_tool_calls(self._call("scan__status"))
        assert render.render_approval_prompt.call_count == 0
        assert result["ok"] is True
        assert result["value"] == {"count": 3}
        assert registry.seen == [("scan.status", {})]

    def test_read_only_capability_completion_lands_on_a_valid_subject(self, scoped):
        agent, _render = self._agent("r")
        landed = []
        agent.bus.subscribe(
            "scan.status.complete", lambda _subject, payload: landed.append(payload)
        )
        [(_tid, _name, result)] = agent._process_tool_calls(self._call("scan__status"))
        assert [p["result"] for p in landed] == [result]

    def test_completion_subject_collapses_the_projected_separator(self):
        from axiom.extensions.builtins.chat.agent import _completion_subject

        assert _completion_subject("write_file") == "write.file.complete"
        assert _completion_subject("scan__status") == "scan.status.complete"

    def test_side_effect_capability_still_prompts_and_r_rejects(self, scoped, registry):
        agent, render = self._agent("r")
        [(_tid, _name, result)] = agent._process_tool_calls(
            self._call("press__draft", {"source": "notes.md"})
        )
        assert render.render_approval_prompt.call_count == 1
        assert result == {"error": "Rejected by user"}
        assert registry.seen == []

    def test_builtin_read_tool_is_unchanged(self, monkeypatch):
        from axiom.extensions.builtins.chat import agent as agent_mod

        ran: list[tuple[str, dict]] = []

        def _fake_execute(name, params):
            ran.append((name, dict(params)))
            return {"providers": []}

        monkeypatch.setattr(agent_mod, "execute_tool", _fake_execute)
        agent, render = self._agent("r")
        [(_tid, _name, result)] = agent._process_tool_calls(self._call("list_providers"))
        assert render.render_approval_prompt.call_count == 0
        assert ran == [("list_providers", {})]
        assert result == {"providers": []}

    def test_builtin_write_tool_still_prompts(self, tmp_path):
        agent, render = self._agent("r")
        target = tmp_path / "never.txt"
        [(_tid, _name, result)] = agent._process_tool_calls(
            self._call("write_file", {"file_path": str(target), "content": "x"})
        )
        assert render.render_approval_prompt.call_count == 1
        assert result == {"error": "Rejected by user"}
        assert not target.exists()

    def test_unknown_tool_name_is_still_write(self):
        agent, render = self._agent("r")
        [(_tid, _name, result)] = agent._process_tool_calls(self._call("no_such_tool"))
        assert render.render_approval_prompt.call_count == 1
        assert result == {"error": "Rejected by user"}

    def test_action_for_reads_the_offered_table_then_the_registry(self):
        from axiom.extensions.builtins.chat.agent import _action_for
        from axiom.extensions.builtins.chat.tools import ToolDef

        table = {
            "scan__status": ToolDef(
                name="scan__status", description="", category=ActionCategory.READ
            )
        }
        assert _action_for("scan__status", {}, table).category is ActionCategory.READ
        # Outside the offered table the registry decides; unknown stays WRITE.
        assert _action_for("scan__status", {}, {}).category is ActionCategory.WRITE
        assert _action_for("query_docs", {}, {}).category is ActionCategory.READ
        assert _action_for("write_file", {}, {}).category is ActionCategory.WRITE
        assert _action_for("no_such_tool", {}, {}).category is ActionCategory.WRITE

    def test_builtin_tool_table_agrees_with_the_action_registry(self):
        """Built-ins keep their registry class: the table never contradicts it."""
        from axiom.extensions.builtins.chat import tools
        from axiom.infra.orchestrator.actions import ACTION_REGISTRY

        table = dict(tools.BUILTIN_TOOLS)
        table.update(tools._scan_extensions())
        disagreeing = {
            name: (tool_def.category, ACTION_REGISTRY[name])
            for name, tool_def in table.items()
            if name in ACTION_REGISTRY and ACTION_REGISTRY[name] is not tool_def.category
        }
        assert disagreeing == {}
