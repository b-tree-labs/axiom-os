# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Every reply says where it came from (ADR-168): the stamp reaches every surface, from the real selection.

The status line used to name `gateway.active_provider`, the first usable provider in the list, so after an
override or a tier-based choice it could name a model that never answered. It now reads what the gateway
actually selected, and carries where that provider runs and where retrieval came from. The rich renderer
also never accepted `tier`, so the agent's call raised TypeError, which the agent swallows: that surface
showed no status line at all.
"""

from __future__ import annotations

import io
import json
from contextlib import redirect_stdout
from types import SimpleNamespace

import pytest

from axiom.extensions.builtins.chat import where
from axiom.extensions.builtins.chat.agent import ChatAgent
from axiom.extensions.builtins.chat.providers.ansi_render import AnsiRenderProvider
from axiom.extensions.builtins.chat.providers.null_render import NullRenderProvider
from axiom.extensions.builtins.chat.providers.sse_render import SseRenderProvider
from axiom.infra.orchestrator.session import Session
from tests.test_gateway import _gateway_with_providers, _make_provider


@pytest.fixture(autouse=True)
def _no_planes(monkeypatch):
    monkeypatch.setattr(
        where,
        "_retrieval_where",
        lambda: where.RetrievalWhere("remote-node", "node.example.edu", "served"),
    )


def _agent_that_answered_with(monkeypatch, name):
    gw = _gateway_with_providers(
        [
            _make_provider(
                name="home", endpoint="http://localhost:8080/v1", api_key_env="", priority=1
            ),
            _make_provider(
                name="node",
                endpoint="https://node.example.edu:3001/v1",
                placement="remote-node",
                node_host="node.example.edu",
                priority=2,
            ),
        ],
        monkeypatch,
    )
    gw.set_provider_override(name)
    gw._select_provider("chat")
    rendered = []

    class R(NullRenderProvider):
        def render_status(self, model, tokens_in, tokens_out, cost, tier=None, where=None):
            rendered.append({"model": model, "tier": tier, "where": where})

    a = ChatAgent.__new__(ChatAgent)
    a._render = R()
    a.gateway = gw
    a.session = Session()
    a.usage = SimpleNamespace(
        turns=[SimpleNamespace(input_tokens=11, output_tokens=22, cost=0.25, latency_ms=412)]
    )
    return a, rendered


def test_the_agent_hands_the_renderer_what_actually_answered_not_the_first_provider(monkeypatch):
    a, rendered = _agent_that_answered_with(monkeypatch, "node")
    a._emit_status(routing_tier="public")
    [r] = rendered
    assert r["where"]["provider"] == "node" and r["where"]["placement"] == "remote-node"
    assert r["where"]["host"] == "node.example.edu" and r["model"] == r["where"]["model"]
    assert a.gateway.active_provider.name == "home", "the old status line would have said home"


def test_the_terminal_stamp_names_the_model_where_it_runs_and_the_retrieval_source(monkeypatch):
    a, _ = _agent_that_answered_with(monkeypatch, "node")
    d = where.to_dict(where.where_report(a))
    buf = io.StringIO()
    with redirect_stdout(buf):
        AnsiRenderProvider().render_status("test-model", 1000, 500, 0.0075, tier="public", where=d)
    out = buf.getvalue()
    assert (
        "REMOTE node.example.edu" in out
        and "RAG: REMOTE node.example.edu" in out
        and "1,000 in" in out
    )


def test_a_local_answer_is_stamped_local(monkeypatch):
    a, _ = _agent_that_answered_with(monkeypatch, "home")
    d = where.to_dict(where.where_report(a))
    buf = io.StringIO()
    with redirect_stdout(buf):
        AnsiRenderProvider().render_status("m", 1, 2, 0.0, where=d)
    assert "LOCAL" in buf.getvalue() and "REMOTE test" not in buf.getvalue()


def test_the_wire_frame_carries_the_structured_where():
    frames: list[dict] = []
    d = {
        "provider": "node",
        "placement": "remote-node",
        "host": "node.example.edu",
        "verified": True,
        "retrieval": {"kind": "local", "host": ""},
    }
    SseRenderProvider(frames.append).render_status("m", 1, 2, 0.0, tier="public", where=d)
    s = frames[0]["status"]
    assert s["where"]["placement"] == "remote-node" and s["where"]["host"] == "node.example.edu"
    json.dumps(frames[0])


def test_the_rich_renderer_accepts_tier_and_where_and_prints_the_stamp(capsys):
    rich = pytest.importorskip("rich")
    del rich
    from axiom.extensions.builtins.chat.providers.rich_render import RichRenderProvider

    p = RichRenderProvider()
    d = {
        "provider": "node",
        "model": "m",
        "placement": "remote-node",
        "host": "node.example.edu",
        "verified": True,
        "retrieval": {"kind": "none", "host": ""},
    }
    p.render_status(
        "m", 1, 2, 0.0, tier="public", where=d
    )  # raised TypeError before: no status line at all
    assert "REMOTE node.example.edu" in capsys.readouterr().out


def test_every_renderer_declares_the_where_parameter():
    import inspect

    from axiom.extensions.builtins.chat.providers import (
        ansi_render,
        base,
        null_render,
        rich_render,
        sse_render,
    )

    for mod, cls in [
        (base, "RenderProvider"),
        (ansi_render, "AnsiRenderProvider"),
        (null_render, "NullRenderProvider"),
        (rich_render, "RichRenderProvider"),
        (sse_render, "SseRenderProvider"),
    ]:
        params = inspect.signature(getattr(mod, cls).render_status).parameters
        assert "where" in params and "tier" in params, f"{cls}.render_status"


def test_the_stamp_survives_a_missing_gateway_without_costing_the_answer():
    a = ChatAgent.__new__(ChatAgent)
    a._render = NullRenderProvider()
    a.gateway = None
    a.usage = SimpleNamespace(turns=[SimpleNamespace(input_tokens=1, output_tokens=1, cost=0.0)])
    a._emit_status(routing_tier=None)  # must not raise
