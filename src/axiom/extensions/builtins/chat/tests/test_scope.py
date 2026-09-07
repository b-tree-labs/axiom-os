# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""One agent, many conversations, nothing shared between them.

A serving worker holds one ``ChatAgent`` and answers many people's requests
with it. Every value that belongs to a conversation rather than to the
process therefore has to live somewhere a request can own: a ``ChatScope``.
The leak these tests exist to catch is the one where person A answers
"always allow" for a write tool and person B's next request runs that tool
with no approval at all, and the same shape of leak for conversation
history, a persisted deny, the interaction mode, queued images and the
workspace brief.

The other half matters just as much: a terminal passes no scope, and every
attribute it has always reached for by name still reads and writes the one
default conversation.
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from axiom.extensions.builtins.chat.agent import ChatAgent
from axiom.extensions.builtins.chat.permissions import ToolPermissions
from axiom.extensions.builtins.chat.scope import ChatScope
from axiom.infra.bus import EventBus
from axiom.infra.gateway import CompletionResponse, Gateway, ToolUseBlock
from axiom.infra.orchestrator.session import Session

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_gateway():
    gw = MagicMock(spec=Gateway)
    gw.available = True
    gw.active_provider = MagicMock()
    gw.active_provider.name = "test"
    gw.active_provider.model = "test-model"
    gw.complete_with_tools.return_value = CompletionResponse(
        text="ok", provider="test", success=True
    )
    return gw


@pytest.fixture
def agent(mock_gateway, tmp_path):
    return ChatAgent(
        gateway=mock_gateway,
        bus=EventBus(log_path=tmp_path / "events.jsonl"),
        session=Session(),
    )


def _write_call(target: Path, tool_id: str = "t1") -> CompletionResponse:
    """A model response asking to write a file (a WRITE tool: it needs approval)."""
    return CompletionResponse(
        text="",
        tool_use=[
            ToolUseBlock(
                tool_id=tool_id,
                name="write_file",
                input={"file_path": str(target), "content": "x"},
            )
        ],
        provider="test",
        success=True,
    )


def _answering(choice: str) -> MagicMock:
    """A render provider that answers every approval prompt with ``choice``."""
    render = MagicMock()
    render.render_approval_prompt.return_value = choice
    return render


# ---------------------------------------------------------------------------
# The leak
# ---------------------------------------------------------------------------


class TestApprovalDoesNotCrossScopes:
    """An approval answered in one conversation binds only that conversation."""

    def test_always_allow_in_one_scope_does_not_auto_approve_in_another(self, agent, tmp_path):
        a, b = ChatScope(), ChatScope()

        agent.set_render_provider(_answering("A"))
        agent._process_tool_calls(_write_call(tmp_path / "a.txt"), scope=a)
        assert (tmp_path / "a.txt").read_text() == "x"
        assert a.permissions.get("write_file") == "allow"

        # B never answered anything: B must be asked, not auto-approved.
        assert b.permissions.get("write_file") == "ask"
        render_b = _answering("r")
        agent.set_render_provider(render_b)
        agent._process_tool_calls(_write_call(tmp_path / "b.txt", "t2"), scope=b)
        render_b.render_approval_prompt.assert_called_once()
        assert not (tmp_path / "b.txt").exists()

    def test_session_allowlist_does_not_cross_scopes(self, agent, tmp_path):
        """The always-approved set is a property of one conversation."""
        a, b = ChatScope(), ChatScope()
        a.allowlist.add("write_file")

        assert "write_file" not in b.allowlist
        render_b = _answering("r")
        agent.set_render_provider(render_b)
        agent._process_tool_calls(_write_call(tmp_path / "b.txt"), scope=b)
        render_b.render_approval_prompt.assert_called_once()
        assert not (tmp_path / "b.txt").exists()

    def test_persisted_deny_does_not_cross_scopes(self, agent, tmp_path):
        """``D`` in one conversation does not silently deny another's call."""
        a, b = ChatScope(), ChatScope()

        agent.set_render_provider(_answering("D"))
        agent._process_tool_calls(_write_call(tmp_path / "a.txt"), scope=a)
        assert a.permissions.get("write_file") == "deny"

        assert b.permissions.get("write_file") == "ask"
        render_b = _answering("a")
        agent.set_render_provider(render_b)
        agent._process_tool_calls(_write_call(tmp_path / "b.txt", "t2"), scope=b)
        render_b.render_approval_prompt.assert_called_once()
        assert (tmp_path / "b.txt").read_text() == "x"

    def test_pending_actions_do_not_cross_scopes(self, agent, tmp_path):
        """The gate holds actions mid-turn, so each conversation gets its own."""
        a, b = ChatScope(), ChatScope()
        assert a.gate is not b.gate

        agent.set_render_provider(_answering("a"))
        agent._process_tool_calls(_write_call(tmp_path / "a.txt"), scope=a)

        assert [act.name for act in a.gate.all_actions()] == ["write_file"]
        assert b.gate.all_actions() == []


class TestConversationDoesNotCrossScopes:
    """History, mode, images and the workspace brief belong to one person."""

    def test_history_added_under_one_scope_is_absent_from_another(self, agent, mock_gateway):
        a, b = ChatScope(), ChatScope()

        agent.turn("a secret question", stream=False, scope=a)

        assert [m.content for m in a.session.messages if m.role == "user"] == ["a secret question"]
        assert b.session.messages == []

    def test_interaction_mode_does_not_cross_scopes(self, agent):
        a, b = ChatScope(), ChatScope()

        agent.set_interaction_mode("ask", scope=a)

        assert a.interaction_mode == "ask"
        assert b.interaction_mode == "agent"

    def test_interaction_mode_of_one_scope_governs_only_its_own_turn(self, agent, mock_gateway):
        """``ask`` strips the tool surface; it must strip only the asker's."""
        a, b = ChatScope(), ChatScope()
        a.interaction_mode = "ask"

        agent.turn("no tools for me", stream=False, scope=a)
        assert mock_gateway.complete_with_tools.call_args.kwargs["tools"] is None

        agent.turn("tools please", stream=False, scope=b)
        assert mock_gateway.complete_with_tools.call_args.kwargs["tools"]

    def test_pending_images_do_not_cross_scopes(self, agent, mock_gateway, tmp_path):
        from axiom.extensions.builtins.chat.attachments import ImageAttachment

        png = tmp_path / "x.png"
        png.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 32)
        a, b = ChatScope(), ChatScope()
        a.pending_images.append(ImageAttachment.from_path(png))

        agent.turn("plain question", stream=False, scope=b)

        sent = mock_gateway.complete_with_tools.call_args.kwargs["messages"]
        assert all(isinstance(m.get("content"), str) for m in sent)
        assert len(a.pending_images) == 1, "B's turn consumed A's queued image"

    def test_workspace_context_does_not_cross_scopes(self, agent):
        a, b = ChatScope(), ChatScope()
        a.workspace_context = "SCOPE-A-ONLY-BRIEF"

        assert "SCOPE-A-ONLY-BRIEF" not in agent._build_system_prompt(scope=b)
        assert "SCOPE-A-ONLY-BRIEF" in agent._build_system_prompt(scope=a)

    def test_session_mode_does_not_cross_scopes(self, agent, mock_gateway):
        """The routing tier one person asked for never re-routes another."""
        a, b = ChatScope(), ChatScope()
        a.session_mode = "export_controlled"

        seen: list[str] = []
        with patch.object(
            agent._router, "classify", side_effect=agent._router.classify
        ) as classify:
            agent.turn("one", stream=False, scope=a)
            agent.turn("two", stream=False, scope=b)
            seen = [c.kwargs["session_mode"] for c in classify.call_args_list]
        assert seen == ["export_controlled", "auto"]

    def test_last_turn_tools_do_not_cross_scopes(self, agent, mock_gateway):
        a, b = ChatScope(), ChatScope()
        mock_gateway.complete_with_tools.side_effect = [
            CompletionResponse(
                text="",
                tool_use=[ToolUseBlock(tool_id="t1", name="list_providers", input={})],
                provider="test",
                success=True,
            ),
            CompletionResponse(text="done", provider="test", success=True),
        ]

        agent.turn("what providers", stream=False, scope=a)

        assert a.last_turn_tools == ["list_providers"]
        assert b.last_turn_tools == []

    def test_cancelling_one_scope_does_not_cancel_another(self, agent):
        a, b = ChatScope(), ChatScope()

        agent.cancel(scope=a)

        assert agent.is_cancelled(scope=a)
        assert not agent.is_cancelled(scope=b)

    def test_a_raw_turn_mutates_no_session_in_any_scope(self, agent, mock_gateway):
        """The benchmarking bypass stays ephemeral whichever scope asked."""
        scope = ChatScope()

        agent.turn("benchmark prompt", stream=False, raw=True, scope=scope)

        assert scope.session.messages == []
        assert agent.session.messages == []


# ---------------------------------------------------------------------------
# A terminal passes no scope and nothing changes
# ---------------------------------------------------------------------------


class TestDefaultScopeIsUnchanged:
    """Passing no scope keeps every existing attribute working by name."""

    def test_public_attributes_still_read_by_name(self, agent):
        assert isinstance(agent.session, Session)
        assert isinstance(agent.permissions, ToolPermissions)
        assert agent.gate is not None
        assert agent.last_turn_tools == []

    def test_private_attributes_still_read_by_name(self, agent):
        assert agent._session_allowlist == set()
        assert agent._session_mode == "auto"
        assert agent._interaction_mode == "agent"
        assert agent._pending_images == []
        assert agent._workspace_context == ""

    def test_every_scoped_attribute_reads_through_the_default_scope(self, agent):
        for agent_name, scope_name in ChatAgent.SCOPED_ATTRIBUTES.items():
            assert getattr(agent, agent_name) is getattr(agent.scope, scope_name)

    def test_assignment_writes_through_to_the_default_scope(self, agent):
        replacement = Session()
        agent.session = replacement
        assert agent.scope.session is replacement

        agent._workspace_context = "brief"
        assert agent.scope.workspace_context == "brief"

        agent._session_mode = "public"
        assert agent.scope.session_mode == "public"

    def test_writing_the_scope_is_seen_through_the_attribute(self, agent):
        agent.scope.workspace_context = "from the scope"
        assert agent._workspace_context == "from the scope"

    def test_session_injected_at_construction_is_the_default_scope_session(self, mock_gateway):
        session = Session()
        built = ChatAgent(gateway=mock_gateway, bus=EventBus(), session=session)
        assert built.session is session
        assert built.scope.session is session

    def test_permissions_injected_at_construction_are_the_default_scope_map(self, mock_gateway):
        perms = ToolPermissions()
        built = ChatAgent(gateway=mock_gateway, bus=EventBus(), permissions=perms)
        assert built.permissions is perms
        assert built.scope.permissions is perms

    def test_default_scope_permissions_are_the_persisted_map(self, agent):
        """A terminal keeps loading the operator's saved allow/deny choices."""
        assert agent.permissions.path is not None
        assert agent.permissions.path.name == "tool_permissions.json"

    def test_a_bare_scope_starts_with_an_in_memory_permission_map(self):
        """A request's approvals start clean and are never written to disk."""
        assert ChatScope().permissions.path is None

    def test_two_sequential_turns_with_no_scope_share_state(self, agent, mock_gateway):
        agent.turn("first", stream=False)
        agent.turn("second", stream=False)
        assert [m.content for m in agent.session.messages if m.role == "user"] == [
            "first",
            "second",
        ]

    def test_a_scoped_turn_leaves_the_default_scope_untouched(self, agent, mock_gateway):
        scope = ChatScope()
        agent.turn("only for this request", stream=False, scope=scope)
        assert agent.session.messages == []
        assert scope.session.messages

    def test_set_interaction_mode_with_no_scope_still_sets_the_default(self, agent):
        agent.set_interaction_mode("plan")
        assert agent._interaction_mode == "plan"
        assert agent.scope.interaction_mode == "plan"

    def test_cancel_with_no_scope_still_cancels_the_default(self, agent):
        agent.cancel()
        assert agent.is_cancelled()
        agent.reset_cancel()
        assert not agent.is_cancelled()

    def test_allowlist_helpers_with_no_scope_act_on_the_default(self, agent):
        agent._session_allowlist.add("write_file")
        assert agent.allowlisted_tools() == ["write_file"]
        agent.revoke_allowlist("write_file")
        assert agent.allowlisted_tools() == []

    def test_allowlist_helpers_take_a_scope(self, agent):
        scope = ChatScope()
        scope.allowlist.add("write_file")
        assert agent.allowlisted_tools(scope=scope) == ["write_file"]
        assert agent.allowlisted_tools() == []
        agent.revoke_allowlist(scope=scope)
        assert scope.allowlist == set()

    def test_an_agent_built_without_init_still_resolves_a_default_scope(self):
        """Surfaces build bare agents; the default exists from first use."""
        bare = ChatAgent.__new__(ChatAgent)
        session = Session()
        bare.session = session
        assert bare.scope.session is session


# ---------------------------------------------------------------------------
# The structural guard: nothing per-conversation may sit on the agent
# ---------------------------------------------------------------------------

# Everything the agent itself is allowed to hold. Each name is here because
# one person's value would be right to use for another person's request:
# that is the whole test.
PROCESS_LEVEL_ATTRIBUTES = {
    # One LLM gateway for the process; every request is routed through it.
    "gateway",
    # The process event bus; subscribers are installed once, not per request.
    "bus",
    # Token and cost meter for this process. No request's behaviour is
    # decided from another request's token count.
    "usage",
    # The render provider a surface installed for all of its requests, the
    # same shape as the approval policy below.
    "_render",
    # The older bare streaming callback; same reason as _render.
    "_renderer_callback",
    # How this surface answers approvals. A surface declares it once, for
    # every request it will ever serve.
    "_approval_policy",
    # Stateless classifier. Holds no conversation.
    "_router",
    # A retrieval store handle, shared by every request; what a person is
    # allowed to see is filtered inside retrieval, not by holding a store.
    "_rag_store",
    # Whether that handle has been built yet.
    "_rag_init_attempted",
    # A memory store handle, like the retrieval store. Whose memory gets read
    # comes from the principal on the scoped session, not from this handle.
    "_composition",
    # The conversation used when a caller passes no scope.
    "_default_scope",
}


def _drive_a_full_turn(agent, mock_gateway, tmp_path):
    """Run a turn that uses a tool and answers an approval, so every
    attribute the turn path can create actually exists on the instance."""
    mock_gateway.complete_with_tools.side_effect = [
        CompletionResponse(
            text="",
            tool_use=[
                ToolUseBlock(
                    tool_id="t1",
                    name="write_file",
                    input={"file_path": str(tmp_path / "s.txt"), "content": "x"},
                )
            ],
            provider="test",
            success=True,
        ),
        CompletionResponse(text="done", provider="test", success=True),
    ]
    agent.set_render_provider(_answering("a"))
    agent.turn("write something", stream=False)


class TestNoRequestStateOnTheAgent:
    """The guard that stops the leak reopening when a field is added."""

    def test_no_attribute_outside_the_documented_process_level_set(
        self, agent, mock_gateway, tmp_path
    ):
        _drive_a_full_turn(agent, mock_gateway, tmp_path)
        unexpected = set(vars(agent)) - PROCESS_LEVEL_ATTRIBUTES
        assert not unexpected, (
            f"{sorted(unexpected)} live on the agent. If a value belongs to one "
            "conversation put it on ChatScope; if it truly belongs to the "
            "process, add it to PROCESS_LEVEL_ATTRIBUTES with the reason."
        )

    def test_no_mutable_container_on_the_agent(self, agent, mock_gateway, tmp_path):
        _drive_a_full_turn(agent, mock_gateway, tmp_path)
        containers = {
            name for name, value in vars(agent).items() if isinstance(value, set | dict | list)
        }
        assert containers == set(), (
            f"{sorted(containers)} is a mutable container on the shared agent"
        )

    def test_every_scoped_attribute_is_a_property_not_instance_state(self, agent):
        for name in ChatAgent.SCOPED_ATTRIBUTES:
            assert isinstance(getattr(ChatAgent, name), property), name
            assert name not in vars(agent), f"{name} shadows its scope property"

    def test_the_scope_carries_the_fields_that_were_moved(self):
        moved = {
            "session",
            "permissions",
            "gate",
            "allowlist",
            "session_mode",
            "interaction_mode",
            "pending_images",
            "workspace_context",
            "last_turn_tools",
            "last_retrieved",
            "last_composer",
            "turn_query",
            "turn_start",
            "cancel_event",
        }
        assert moved <= set(vars(ChatScope()))


class TestScopeIsThreadedExplicitly:
    """Explicit at the call site, never ambient."""

    def _source(self, module_name: str) -> str:
        import importlib

        return Path(inspect.getfile(importlib.import_module(module_name))).read_text(
            encoding="utf-8"
        )

    def test_no_context_variable_carries_the_scope(self):
        """An ambient context variable would reproduce the invisibility that
        made this a leak in the first place, so neither module may import one."""
        for module in (
            "axiom.extensions.builtins.chat.agent",
            "axiom.extensions.builtins.chat.scope",
        ):
            assert "contextvars" not in self._source(module)
            assert "threading.local" not in self._source(module)

    def test_every_internal_call_passes_the_scope(self):
        """A method that takes a scope is never called from inside the agent
        without one: that is what makes the threading leak-proof."""
        source = self._source("axiom.extensions.builtins.chat.agent")
        tree = ast.parse(source)
        agent_cls = next(
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.ClassDef) and node.name == "ChatAgent"
        )
        takes_scope = {
            node.name
            for node in agent_cls.body
            if isinstance(node, ast.FunctionDef)
            and any(a.arg == "scope" for a in node.args.kwonlyargs + node.args.args)
        }
        assert "turn" in takes_scope, "the public entry point must take a scope"

        missing = []
        for node in ast.walk(agent_cls):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if not isinstance(func, ast.Attribute):
                continue
            if not (isinstance(func.value, ast.Name) and func.value.id == "self"):
                continue
            if func.attr not in takes_scope:
                continue
            if not any(kw.arg == "scope" for kw in node.keywords):
                missing.append(f"{func.attr} at line {node.lineno}")
        assert not missing, f"called without an explicit scope: {missing}"
