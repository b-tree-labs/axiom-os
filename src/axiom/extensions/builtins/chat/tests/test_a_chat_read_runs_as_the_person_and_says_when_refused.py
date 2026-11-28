# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""A data read from chat runs as the person on the chat surface, and a refusal says so.

Seen live: chat built its skill context with no surface and no principal, so the serving guard
(ADR-157) read every chat call as anonymous and gave it the smallest budget, 0.2 requests a
second. A chart question makes two or three reads in a turn, so the second was refused. The tool
event then reported it as ok, because only an ``error`` key counted as a failure and a refused
capability answers ``{"ok": false, "errors": [...]}``. A state that lies (design principle 4).
"""

from __future__ import annotations

import pytest

from axiom.extensions.builtins.chat import tools as chat_tools
from axiom.infra.skills import SkillRegistry, SkillResult, SkillSpec

SEEN: list = []


def _spec(fn) -> SkillSpec:
    return SkillSpec(
        name="data.series", fn=fn, description="a series", surfaces=("agent_tool",),
        side_effects=False, inputs={"channel": "str"},
    )


@pytest.fixture
def wired(monkeypatch):
    def wire(fn):
        registry = SkillRegistry()
        registry.register_skill(_spec(fn), mutating=False)
        monkeypatch.setattr(chat_tools, "_skill_registry", lambda: registry)
        monkeypatch.setattr(chat_tools, "_skill_namespaces", lambda: ["data"])
        monkeypatch.setattr(chat_tools, "_offer_node_data_tools", lambda: True)
        monkeypatch.setattr(chat_tools, "_bound_tools", lambda: {})
        SEEN.clear()

    return wire


def _turn(principal: str = "@pat:example"):
    from unittest.mock import MagicMock

    from axiom.extensions.builtins.chat.agent import ChatAgent
    from axiom.extensions.builtins.chat.providers.sse_render import SseRenderProvider
    from axiom.infra.bus import EventBus
    from axiom.infra.gateway import CompletionResponse, Gateway, ToolUseBlock
    from axiom.infra.orchestrator.session import Session

    gw = MagicMock(spec=Gateway)
    gw.available = True
    agent = ChatAgent(gateway=gw, bus=EventBus(), session=Session(principal_id=principal))
    frames: list[dict] = []
    agent.set_render_provider(SseRenderProvider(frames.append))
    call = CompletionResponse(
        text="", provider="test", success=True,
        tool_use=[ToolUseBlock(tool_id="t1", name="data__series", input={"channel": "a"})],
    )
    results = agent._process_tool_calls(call)
    outcome = next(f["tool_outcome"] for f in frames if "tool_outcome" in f)
    return results[0][2], outcome


def test_a_chat_read_runs_as_the_signed_in_person_on_the_chat_surface(wired):
    from axiom.infra.serving_guard import caller_class

    def record(params, ctx):
        SEEN.append(ctx)
        return SkillResult(ok=True, value={"data": {"series": []}})

    wired(record)
    _turn("@pat:example")
    [ctx] = SEEN
    assert ctx.surface == "chat"
    assert ctx.principal.handle == "@pat:example"
    assert caller_class(surface=ctx.surface, assured=False) == "interactive"


def test_a_refused_read_is_reported_as_refused_to_the_model_and_in_the_tool_event(wired):
    def refuse(params, ctx):
        return SkillResult(ok=False, errors=["rate limit: 0.2/s for anonymous callers"])

    wired(refuse)
    result, outcome = _turn()
    assert "rate limit" in result["error"]
    assert outcome["ok"] is False
    assert "rate limit" in outcome["error"]


def test_no_exception_text_from_a_tool_reaches_the_model_or_the_page(wired):
    """Seen live: a read crashed underneath, the skill wrapper returned the exception and a
    traceback as its errors, and the model explained the Python error to the person, with code.
    What reaches the model and the tool event is a short, plain refusal; the detail stays in the log."""
    import json

    def crash(params, ctx):
        {}["x"].get("y")  # noqa: B018 - a crash inside a read
        return SkillResult(ok=True)

    def crash_reported(params, ctx):
        return SkillResult(ok=False, errors=[
            "AttributeError: 'str' object has no attribute 'get'; Skill data.series failed "
            "unexpectedly. Re-run with AXIOM_DEBUG=1 to see the traceback.",
            'Traceback (most recent call last):\n  File "gold.py", line 349, in series\n',
        ])

    for fn in (crash, crash_reported):
        wired(fn)
        result, outcome = _turn()
        text = json.dumps(result) + json.dumps(outcome)
        for leak in ("Traceback", "Error", "attribute", ".py", "AXIOM_DEBUG", "line 349", "KeyError"):
            assert leak not in text, (fn.__name__, leak, text)
        assert result["error"].startswith("this call failed")
        assert outcome["ok"] is False
