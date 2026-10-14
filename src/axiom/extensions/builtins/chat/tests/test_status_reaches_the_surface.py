# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Which model answered, at what tier, and what it cost — on every surface.

`render_status` had exactly one caller, in the terminal CLI, and the web
provider stubbed the method to a no-op. So a browser user could not see which
model answered them, which routing tier it came from, or what the turn cost,
while a terminal user saw all of it. The routing tier is the knowledge dial;
not showing it is withholding the single most load-bearing fact about an
answer.

The fix follows the citations shape: the AGENT emits once on the completed-turn
path, so every provider gets it, rather than one surface calling it for itself.
"""

from __future__ import annotations

from unittest.mock import MagicMock


class TestTheProtocolCarriesTheTier:
    def test_render_status_accepts_a_tier(self):
        """ansi already took a `tier` kwarg the protocol did not declare, so
        no caller could rely on it. The protocol now declares it."""
        import inspect

        from axiom.extensions.builtins.chat.providers.base import RenderProvider

        assert "tier" in inspect.signature(RenderProvider.render_status).parameters


class TestTheWebProviderPutsItOnTheWire:
    def _emitted(self, **kw):
        from axiom.extensions.builtins.chat.providers.sse_render import SseRenderProvider

        frames: list[dict] = []
        SseRenderProvider(frames.append).render_status(**kw)
        return frames

    def test_a_status_frame_carries_model_tokens_cost_and_tier(self):
        [f] = self._emitted(model="m-1", tokens_in=10, tokens_out=20, cost=0.5, tier="deep")
        s = f["status"]
        assert s["model"] == "m-1"
        assert s["tokens_in"] == 10
        assert s["tokens_out"] == 20
        assert s["cost"] == 0.5
        assert s["tier"] == "deep"

    def test_a_turn_with_nothing_to_report_emits_nothing(self):
        """No model and no tokens is not a status line reading '0 in, 0 out'."""
        assert self._emitted(model="", tokens_in=0, tokens_out=0, cost=0.0) == []

    def test_tokens_alone_are_worth_reporting(self):
        assert self._emitted(model="", tokens_in=5, tokens_out=1, cost=0.0)

    def test_the_frame_survives_json(self):
        import json

        [f] = self._emitted(model="m", tokens_in=1, tokens_out=1, cost=0.1, tier="fast")
        assert json.loads(json.dumps(f))["status"]["tier"] == "fast"


class TestTheAgentEmitsItForEverySurface:
    def _agent(self):
        from axiom.extensions.builtins.chat.agent import ChatAgent

        a = ChatAgent.__new__(ChatAgent)
        a._render = MagicMock()
        a.gateway = MagicMock()
        a.gateway.active_provider.model = "m-1"
        a.usage = MagicMock()
        a.usage.turns = [MagicMock(input_tokens=11, output_tokens=22, cost=0.25)]
        return a

    def test_it_hands_the_turn_numbers_to_the_renderer(self):
        a = self._agent()
        a._emit_status(routing_tier="deep")
        a._render.render_status.assert_called_once()
        kw = a._render.render_status.call_args.kwargs
        assert kw["model"] == "m-1"
        assert kw["tokens_in"] == 11
        assert kw["tokens_out"] == 22
        assert kw["cost"] == 0.25
        assert kw["tier"] == "deep"

    def test_no_recorded_turn_emits_nothing(self):
        a = self._agent()
        a.usage.turns = []
        a._emit_status(routing_tier="deep")
        a._render.render_status.assert_not_called()

    def test_a_failing_renderer_never_breaks_the_answer(self):
        a = self._agent()
        a._render.render_status.side_effect = RuntimeError("terminal gone")
        a._emit_status(routing_tier="deep")  # must not raise

    def test_the_finalize_path_calls_it(self):
        import inspect

        from axiom.extensions.builtins.chat.agent import ChatAgent

        assert "_emit_status" in inspect.getsource(ChatAgent._turn_impl)


class TestTheTerminalDoesNotDoubleReport:
    def test_the_cli_no_longer_renders_its_own_status_line(self):
        """The agent emits for every surface now. A CLI that also calls it
        prints the line twice."""
        import inspect

        from axiom.extensions.builtins.chat import cli

        assert "render_status(" not in inspect.getsource(cli)
