# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""A chat turn on a model too small for the tool prompt says so, and every turn ends with its footer.

Found live on a fresh install with a 4,096-token local model: the tool prompt is about 13k tokens, the
server refused it, and the non-streaming turn came back with an answer the model invented after the
gateway quietly dropped the tools. It read as tool-cited and had no footer, so nothing told the reader
which model said it. The footer (``render_status``: model, tokens, cost, tier, where it ran) was emitted
only on the completed-answer path; a turn ending on rounds exhausted, an unrunnable tool attempt or the
deadline printed none.
"""

from __future__ import annotations

import io
import json
from contextlib import redirect_stdout
from unittest.mock import MagicMock, patch

import pytest
import requests

from axiom.extensions.builtins.chat.agent import ChatAgent
from axiom.infra.bus import EventBus
from axiom.infra.gateway import CompletionResponse, Gateway, ToolUseBlock
from axiom.infra.orchestrator.session import Session
from axiom.llm import gateway as gw

_REFUSAL = json.dumps(
    {"error": {"code": 400, "type": "exceed_context_size_error", "n_prompt_tokens": 12932, "n_ctx": 4096}}
)
_INVENTED = "Per the latest reading [C1], the value is 950."


def _http_error(body: str) -> requests.HTTPError:
    resp = requests.Response()
    resp.status_code = 400
    resp._content = body.encode()
    return requests.HTTPError("400 Client Error", response=resp)


class _SseOk:
    """A tool-less answer, for both the streaming and the non-streaming call."""

    encoding = "utf-8"

    def json(self):
        return {"choices": [{"message": {"content": _INVENTED}, "finish_reason": "stop"}], "usage": {}}

    def iter_lines(self, decode_unicode=True):
        yield "data: " + json.dumps({"choices": [{"delta": {"content": _INVENTED}}]})
        yield "data: [DONE]"


def _rendering_agent(gateway, tmp_path):
    agent = ChatAgent(gateway=gateway, bus=EventBus(log_path=tmp_path / "e.jsonl"), session=Session())
    agent._rag_init_attempted = True  # no retrieval store: this test needs no database
    agent._rag_store = None
    render = MagicMock()
    render.stream_text.side_effect = lambda chunks: "".join(c.text for c in chunks if c.type == "text")
    agent.set_render_provider(render)
    return agent, render


@pytest.fixture
def small_local_model(monkeypatch):
    """A real gateway over one local model whose server refuses anything carrying the tool prompt."""

    def post(_requests, url, payload, headers, timeout=60, **kw):
        if payload.get("tools"):
            raise _http_error(_REFUSAL)
        return _SseOk()

    monkeypatch.setattr(gw, "_post_with_rate_limit_retry", post)
    with patch.object(gw.Gateway, "_load_config", lambda self: None):
        g = gw.Gateway()
    g.providers = [
        gw.LLMProvider(
            name="local", endpoint="http://127.0.0.1:1", model="tiny-1b", api_key_env="",
            routing_tier="public", placement="local",
        )
    ]
    return g


@pytest.mark.parametrize("stream", [False, True], ids=["non-streaming", "streaming"])
def test_the_turn_says_the_model_is_too_small_instead_of_inventing(small_local_model, tmp_path, stream):
    agent, render = _rendering_agent(small_local_model, tmp_path)
    answer = agent.turn("what is the value right now?", stream=stream)
    assert _INVENTED not in answer
    assert "tiny-1b" in answer and "4,096" in answer and "12,932" in answer
    assert "connect list" in answer


@pytest.mark.parametrize("stream", [False, True], ids=["non-streaming", "streaming"])
def test_the_footer_prints_on_the_too_small_path(small_local_model, tmp_path, stream):
    agent, render = _rendering_agent(small_local_model, tmp_path)
    agent.turn("what is the value right now?", stream=stream)
    render.render_status.assert_called_once()
    assert render.render_status.call_args.kwargs["model"] == "tiny-1b"


# ---- the footer on every other way a turn that reached a model can end -----------------------------


def _mock_agent(tmp_path, respond):
    g = MagicMock(spec=Gateway)
    g.available = True
    g.active_provider = MagicMock()
    g.active_provider.name = "test"
    g.active_provider.model = "test-model"
    g.complete_with_tools.side_effect = respond
    agent, render = _rendering_agent(g, tmp_path)
    return agent, render


def test_the_footer_prints_when_the_rounds_run_out(tmp_path):
    def respond(*a, **k):
        return CompletionResponse(
            tool_use=[ToolUseBlock(tool_id="t", name="no_such_tool", input={})],
            provider="test", model="test-model", success=True,
        )

    agent, render = _mock_agent(tmp_path, respond)
    agent.turn("q", stream=False)
    render.render_status.assert_called_once()


def test_the_footer_prints_when_the_model_writes_a_tool_call_that_cannot_run(tmp_path, monkeypatch):
    from axiom.extensions.builtins.chat import tool_call_recovery

    monkeypatch.setattr(tool_call_recovery, "recover_tool_calls", lambda text, tools: [])
    monkeypatch.setattr(tool_call_recovery, "looks_like_a_tool_attempt", lambda text, tools: True)

    def respond(*a, **k):
        return CompletionResponse(text='{"name": "made_up"}', provider="test", model="test-model", success=True)

    agent, render = _mock_agent(tmp_path, respond)
    agent.turn("q", stream=False)
    render.render_status.assert_called_once()


def test_an_unpriced_turn_still_prints_its_footer():
    """``cost=None`` is an unpriced provider; comparing it to 0 raised and the agent swallowed the footer."""
    from axiom.extensions.builtins.chat.providers.ansi_render import AnsiRenderProvider

    out = io.StringIO()
    with redirect_stdout(out):
        AnsiRenderProvider().render_status(model="m-1", tokens_in=1, tokens_out=2, cost=None, tier="public")
    assert "m-1" in out.getvalue() and "unpriced" in out.getvalue()
