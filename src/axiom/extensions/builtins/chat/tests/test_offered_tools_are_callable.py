# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""A tool the model is offered is a tool the model can call.

A new user on a laptop asked "who are you?" and the first thing chat printed
was::

    x node_describe failed (3.9s): Unknown tool: node_describe

and, for "max operating power for the last day?"::

    x telemetry_metrics failed (2.2s): Unknown tool: telemetry_metrics

Both tools were in the table the model was offered. Manifest-declared tools
(``[[extension.provides]] kind = "tool"``) were scanned into that table with a
bound handler, but ``execute_tool`` never consulted it: it tried ``tools_ext/``,
the opt-in ``chat_tools_module``, capability projections and a fixed if-chain,
and fell through to "Unknown tool". Every test of the manifest bridge checked
the scan and the binding; none called the tool through the door chat uses.

A model that asks for a name nobody registered gets a corrective answer it can
act on, not a red failure line as the user's first sight of the product.
"""

from __future__ import annotations

import textwrap

import pytest

from axiom.extensions.builtins.chat import tools as T
from axiom.infra.paths import project_dir_name


@pytest.fixture
def project_with_a_declared_tool(tmp_path, monkeypatch):
    """A project-local extension declaring one read tool, with a real handler."""
    handlers = tmp_path / "pkgs"
    handlers.mkdir()
    (handlers / "offered_tool_handlers.py").write_text(
        textwrap.dedent(
            """
            def describe(args):
                return {"ok": True, "seen": dict(args), "answer": "from the handler"}
            """
        )
    )
    monkeypatch.syspath_prepend(str(handlers))

    ext = tmp_path / "proj" / project_dir_name() / "extensions" / "offered_demo"
    ext.mkdir(parents=True)
    (ext / "axiom-extension.toml").write_text(
        textwrap.dedent(
            """
            [extension]
            name = "offered_demo"
            version = "0.1.0"

            [[extension.provides]]
            kind = "tool"
            name = "offered_demo_describe"
            entry = "offered_tool_handlers:describe"
            description = "Describe the demo."
            idempotent = true
            """
        ).strip()
    )
    monkeypatch.setenv("AXIOM_ROOT", str(tmp_path / "proj"))
    return "offered_demo_describe"


class TestADeclaredToolIsCallable:
    def test_it_is_offered(self, project_with_a_declared_tool):
        offered = {d["function"]["name"] for d in T.get_tool_definitions()}
        assert project_with_a_declared_tool in offered

    def test_calling_it_reaches_its_handler(self, project_with_a_declared_tool):
        result = T.execute_tool(project_with_a_declared_tool, {"site": "alpha"})
        assert "error" not in result, result
        assert result["answer"] == "from the handler"
        assert result["seen"] == {"site": "alpha"}


class TestThePlatformsOwnSelfDescriptionIsCallable:
    """The exact call the first user saw fail, against the real registry."""

    def test_node_describe_is_offered(self):
        assert "node_describe" in T.get_all_tools()

    def test_node_describe_dispatches(self):
        result = T.execute_tool("node_describe", {})
        assert not str(result.get("error", "")).startswith("Unknown tool"), result


class TestAnUnregisteredNameIsCorrected:
    """A small model invents tool names. The answer must steer it back."""

    def test_the_answer_names_close_registered_tools(self, project_with_a_declared_tool):
        result = T.execute_tool("offered_demo_describ", {})
        assert "error" in result
        assert project_with_a_declared_tool in result["error"]


class _RecordingRender:
    """Records what the user would be shown. Built on the null surface."""

    def __new__(cls):
        from axiom.extensions.builtins.chat.providers.null_render import (
            NullRenderProvider,
        )

        class _Rec(NullRenderProvider):
            def __init__(self):
                super().__init__()
                self.shown: list[tuple[str, dict]] = []

            def render_tool_start(self, name, params):
                self.shown.append(("start", {"name": name}))

            def render_tool_result(self, name, result, elapsed):
                self.shown.append((name, result))

        return _Rec()


class TestAnInventedNameNeverReachesTheUser:
    """The model asking for a tool that does not exist is the model's mistake
    to correct, not a failure to paint in red as the user's first sight of
    the product."""

    def test_the_call_is_answered_to_the_model_not_rendered(self, tmp_path):
        from unittest.mock import MagicMock

        from axiom.extensions.builtins.chat.agent import ChatAgent
        from axiom.infra.bus import EventBus
        from axiom.infra.gateway import CompletionResponse, Gateway, ToolUseBlock
        from axiom.infra.orchestrator.session import Session

        render = _RecordingRender()
        agent = ChatAgent(
            gateway=MagicMock(spec=Gateway),  # the LLM boundary; never called here
            bus=EventBus(log_path=tmp_path / "events.jsonl"),
            session=Session(),
            render=render,
        )
        response = CompletionResponse(
            text="",
            tool_use=[ToolUseBlock(tool_id="t1", name="node_describ", input={})],
            provider="test",
            success=True,
        )
        results = agent._process_tool_calls(response, T.get_all_tools())

        assert render.shown == [], "an invented tool name was painted for the user"
        (_tid, name, result) = results[0]
        assert name == "node_describ"
        assert "node_describe" in result["error"], result
