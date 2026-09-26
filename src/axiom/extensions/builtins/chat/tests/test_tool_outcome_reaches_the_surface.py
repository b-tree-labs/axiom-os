# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""A browser can tell whether a tool succeeded.

The web provider emitted ``{"tool_result": name}`` and nothing else, so a
browser user saw that a tool ran and could not tell a success from a failure.
The terminal has always shown the difference — a tick or a cross, the elapsed
time, and the error text when there is one.

What is deliberately NOT added: the tool's params and its full result dict.
The terminal does not show those either (``render_tool_start`` prints only
"[calling name...]", and a result is reduced to ok/failed plus the error
string), and a served surface answers people who are not the operator, so
piping a raw result dict to a browser would be a disclosure this does not need
to make for parity.
"""

from __future__ import annotations


def _frames(fn_name, *args, **kw):
    from axiom.extensions.builtins.chat.providers.sse_render import SseRenderProvider

    out: list[dict] = []
    getattr(SseRenderProvider(out.append), fn_name)(*args, **kw)
    return out


class TestTheOldFrameStillTravelsSoAnOlderClientKeepsWorking:
    """A node can be upgraded ahead of the web app that talks to it — a site
    pins its own web version — so the wire change is additive, not a reshape.
    An older client reading `tool_result` as a string still gets a tool name
    rather than "[object Object]"."""

    def test_tool_result_is_still_the_bare_name(self):
        [f] = _frames("render_tool_result", "get_weather", {"temp": 20}, 1.0)
        assert f["tool_result"] == "get_weather"

    def test_the_outcome_rides_alongside_in_its_own_key(self):
        [f] = _frames("render_tool_result", "get_weather", {"temp": 20}, 1.0)
        assert f["tool_outcome"]["name"] == "get_weather"


class TestAFailedToolLooksDifferentFromASucceededOne:
    def test_a_success_reports_ok(self):
        [f] = _frames("render_tool_result", "get_weather", {"temp": 20}, 1.25)
        assert f["tool_outcome"]["ok"] is True

    def test_a_failure_reports_not_ok(self):
        [f] = _frames("render_tool_result", "get_weather", {"error": "no network"}, 0.5)
        assert f["tool_outcome"]["ok"] is False

    def test_a_failure_carries_the_error_text_the_terminal_shows(self):
        [f] = _frames("render_tool_result", "get_weather", {"error": "no network"}, 0.5)
        assert f["tool_outcome"]["error"] == "no network"

    def test_a_success_carries_no_error_key(self):
        [f] = _frames("render_tool_result", "get_weather", {"temp": 20}, 1.0)
        assert "error" not in f["tool_outcome"]


class TestTheFrameCarriesWhatTheTerminalShows:
    def test_the_name(self):
        [f] = _frames("render_tool_result", "get_weather", {}, 1.0)
        assert f["tool_outcome"]["name"] == "get_weather"

    def test_the_elapsed_time(self):
        [f] = _frames("render_tool_result", "get_weather", {}, 1.25)
        assert f["tool_outcome"]["elapsed"] == 1.25

    def test_the_frame_survives_json(self):
        import json

        [f] = _frames("render_tool_result", "t", {"error": "x"}, 0.1)
        assert json.loads(json.dumps(f))["tool_outcome"]["ok"] is False


class TestTheResultItselfNeverLeavesTheNode:
    """Parity does not require disclosing what a tool returned."""

    def test_the_result_payload_is_not_in_the_frame(self):
        [f] = _frames(
            "render_tool_result", "lookup", {"ssn": "123-45-6789", "rows": [1, 2, 3]}, 1.0
        )
        blob = repr(f)
        assert "123-45-6789" not in blob
        assert "rows" not in blob

    def test_tool_params_are_not_in_the_call_frame(self):
        [f] = _frames("render_tool_start", "lookup", {"query": "secret-project"})
        assert "secret-project" not in repr(f)
