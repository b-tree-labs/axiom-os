# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Test mode (invented sample data) asks before an action that only makes sense on a real site.

Reading invented numbers is what test mode is for. Sending an operator an alert, pushing a reading
to a node or changing a setpoint is not: it means something only against a real site. So the call
asks every time, a standing "always allow" does not answer it, and the reason is said first.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from axiom.extensions.builtins.chat import real_site_actions as rsa
from axiom.extensions.builtins.chat.tools import ToolDef
from axiom.infra.orchestrator.actions import ActionCategory
from axiom.infra.serving_mode import SAMPLE_DATA_ENV


def _tool(name, category=ActionCategory.WRITE, **kw):
    return ToolDef(name=name, description="d", category=category, parameters={}, **kw)


TABLE = {
    "alerts__send": _tool("alerts__send"),
    "alerts__list": _tool("alerts__list", ActionCategory.READ),
    "write_file": _tool("write_file"),
    "my_push": _tool("my_push", real_site_only=True),
}


@pytest.fixture(autouse=True)
def _no_sample_env(monkeypatch):
    monkeypatch.delenv(SAMPLE_DATA_ENV, raising=False)


def test_a_write_in_a_site_namespace_acts_on_a_real_site():
    assert rsa.acts_on_a_real_site("alerts__send", TABLE)


def test_a_tool_can_declare_it():
    assert rsa.acts_on_a_real_site("my_push", TABLE)


def test_a_read_never_does_even_in_a_site_namespace():
    assert not rsa.acts_on_a_real_site("alerts__list", TABLE)


def test_an_ordinary_write_does_not():
    assert not rsa.acts_on_a_real_site("write_file", TABLE)


def test_it_only_binds_in_test_mode(monkeypatch):
    assert not rsa.must_ask_in_test_mode("alerts__send", TABLE)
    monkeypatch.setenv(SAMPLE_DATA_ENV, "1")
    assert rsa.must_ask_in_test_mode("alerts__send", TABLE)
    assert not rsa.must_ask_in_test_mode("write_file", TABLE)


# ---- in the chat loop -------------------------------------------------------------------------


def _agent(monkeypatch, answer):
    from axiom.extensions.builtins.chat import agent as agent_mod
    from axiom.infra.bus import EventBus
    from axiom.infra.gateway import Gateway
    from axiom.infra.orchestrator.session import Session

    ran = []
    monkeypatch.setattr(agent_mod, "get_all_tools", lambda: dict(TABLE))
    monkeypatch.setattr(
        agent_mod, "execute_tool", lambda name, params, *a, **k: ran.append(name) or {"ok": True}
    )
    gw = MagicMock(spec=Gateway)
    gw.available = True
    gw.active_provider = MagicMock()
    gw.active_provider.name = "test"
    gw.active_provider.model = "test-model"
    agent = agent_mod.ChatAgent(gateway=gw, bus=EventBus(), session=Session())
    render = MagicMock()
    render.render_approval_prompt.return_value = answer
    agent.set_render_provider(render)
    return agent, render, ran


def _call(name):
    from axiom.infra.gateway import CompletionResponse, ToolUseBlock

    return CompletionResponse(
        text="",
        provider="test",
        success=True,
        tool_use=[ToolUseBlock(tool_id="t1", name=name, input={})],
    )


def test_a_real_site_action_asks_in_test_mode_and_says_why(monkeypatch):
    monkeypatch.setenv(SAMPLE_DATA_ENV, "1")
    agent, render, ran = _agent(monkeypatch, "r")
    results = agent._process_tool_calls(_call("alerts__send"))
    render.render_approval_prompt.assert_called_once()
    warning = [c.args[1] for c in render.render_message.call_args_list if c.args[0] == "system"]
    assert any("TEST MODE" in w and "alerts__send" in w and "real site" in w for w in warning)
    assert "Rejected" in results[0][2]["error"] and ran == []


def test_a_standing_always_allow_does_not_answer_it(monkeypatch):
    monkeypatch.setenv(SAMPLE_DATA_ENV, "1")
    agent, render, ran = _agent(monkeypatch, "a")
    from axiom.extensions.builtins.chat.agent import _active_scope

    scope = _active_scope(agent, None)
    scope.permissions.set("alerts__send", "allow")
    scope.allowlist.add("alerts__send")
    agent._process_tool_calls(_call("alerts__send"))
    render.render_approval_prompt.assert_called_once()
    assert ran == ["alerts__send"]


def test_always_is_taken_as_this_once(monkeypatch):
    monkeypatch.setenv(SAMPLE_DATA_ENV, "1")
    agent, render, ran = _agent(monkeypatch, "A")
    agent._process_tool_calls(_call("alerts__send"))
    agent._process_tool_calls(_call("alerts__send"))
    assert render.render_approval_prompt.call_count == 2  # asked again: no standing yes was kept
    assert ran == ["alerts__send", "alerts__send"]


def test_outside_test_mode_nothing_changes(monkeypatch):
    agent, render, ran = _agent(monkeypatch, "A")
    agent._process_tool_calls(_call("alerts__send"))
    agent._process_tool_calls(_call("alerts__send"))
    assert render.render_approval_prompt.call_count == 1  # "always" was kept, as before
    assert not any("TEST MODE" in str(c) for c in render.render_message.call_args_list)
    assert ran == ["alerts__send", "alerts__send"]
