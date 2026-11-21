# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""The SSE render provider maps agent events → appkit ChatFrames (C2 ③a).

The frame keys here MUST match what appkit's framesFromSSEData reads: chunk,
tool_call, tool_result, action_result, clarification.
"""

from __future__ import annotations

from types import SimpleNamespace

from axiom.extensions.builtins.chat.providers.sse_render import SseRenderProvider
from axiom.infra.orchestrator.actions import Action, ActionStatus


def _chunk(text, kind="text"):
    return SimpleNamespace(type=kind, text=text)


def test_stream_text_emits_chunks_and_accumulates():
    frames: list = []
    p = SseRenderProvider(frames.append)
    out = p.stream_text(iter([_chunk("Hel"), _chunk("lo"), _chunk("", kind="tool_use")]))
    assert out == "Hello"
    assert frames == [{"chunk": "Hel"}, {"chunk": "lo"}]  # non-text chunk emits nothing


def test_tool_frames():
    frames: list = []
    p = SseRenderProvider(frames.append)
    p.render_tool_start("get_weather", {"loc": "TX"})
    p.render_tool_result("get_weather", {"ok": True}, 0.12)
    # The result frame carries the OUTCOME. It used to be the bare name, which
    # left a browser unable to tell a success from a failure; the params and the
    # result dict stay on the node either way.
    assert frames == [
        {"tool_call": "get_weather"},
        {
            "tool_result": "get_weather",
            "tool_outcome": {"name": "get_weather", "ok": True, "elapsed": 0.12},
        },
    ]


def test_approval_emits_clarification_and_returns_a_decision():
    frames: list = []
    p = SseRenderProvider(frames.append)
    decision = p.render_approval_prompt(Action(name="doc.publish", params={"source": "x.md"}))
    clar = frames[0]["clarification"]
    assert clar["kind"] == "approval"
    assert clar["action"]["name"] == "doc.publish"
    assert str(decision) in {"a", "A", "r"}  # answered by policy, never a prompt


def test_action_result_frame_is_json_safe():
    frames: list = []
    p = SseRenderProvider(frames.append)
    action = Action(name="sense.ingest")
    action.status = ActionStatus.COMPLETED
    p.render_action_result(action)
    ar = frames[0]["action_result"]
    assert ar["name"] == "sense.ingest"
    assert ar["status"] == ActionStatus.COMPLETED.value  # enum → its value, not the object


def test_render_message_streams_only_assistant():
    frames: list = []
    p = SseRenderProvider(frames.append)
    p.render_message("assistant", "done")
    p.render_message("user", "ignored")
    p.render_message("assistant", "")
    assert frames == [{"chunk": "done"}]


def test_terminal_chrome_emits_nothing():
    """Only furniture a browser draws for itself stays off the wire.

    Two methods qualify: a welcome banner and a saved-session table. Both are
    console decoration a web surface renders its own way, from its own data.

    Everything else on the protocol is a FACT ABOUT THE ANSWER and belongs on
    the wire. `render_status` (which model, which tier, what it cost) and
    `render_thinking` (the reasoning) were both stubbed here and both grouped
    under "chrome"; that grouping is what made the terminal a richer chat
    client than the web app. The line is drawn at "does a browser draw this
    itself", not at "is it extra".
    """
    frames: list = []
    p = SseRenderProvider(frames.append)
    p.render_welcome()
    p.render_session_list([])
    assert frames == []


def test_status_is_not_chrome_and_does_reach_the_wire():
    frames: list = []
    SseRenderProvider(frames.append).render_status("m", 1, 2, 0.0, tier="deep")
    assert frames and frames[0]["status"]["tier"] == "deep"


def test_thinking_is_not_chrome_and_does_reach_the_wire():
    frames: list = []
    SseRenderProvider(frames.append).render_thinking("weighing options", collapsed=True)
    assert frames and frames[0]["thinking"]["text"] == "weighing options"


# --- the document a verb produced reaches the browser ----------------------
#
# The frame carried a name and a status and not the result, so a capability
# that answered with a table sent the browser the news that a table had been
# computed and never the table. Every tabular answer arrived as prose about
# one.

def _completed(name="daq.table", result=None):
    return Action(name=name, status=ActionStatus.COMPLETED, result=result)


def _frame(action):
    frames: list = []
    SseRenderProvider(frames.append).render_action_result(action)
    return frames[0]["action_result"]


def test_a_table_a_verb_produced_rides_to_the_browser():
    envelope = {
        "spec": {"kind": "rows", "title": "readings", "columns": [{"id": "v", "label": "V"}]},
        "rows": [{"v": 51.2}],
        "matched": 1,
        "fetched": 6000,
    }
    got = _frame(_completed(result=envelope))
    assert got["result"] == envelope
    assert got["result"]["rows"] == [{"v": 51.2}]


def test_an_action_with_no_result_says_nothing_extra():
    got = _frame(_completed(result=None))
    assert "result" not in got and "result_omitted" not in got


def test_a_result_that_is_not_json_is_left_behind_and_said_so():
    """Coercing it would hand the browser something shaped like data."""
    got = _frame(_completed(result={"when": object()}))
    assert "result" not in got
    assert "not JSON" in got["result_omitted"]


def test_a_result_too_large_to_stream_says_that_rather_than_arriving_cut_down():
    got = _frame(_completed(result={"rows": ["x" * 1024] * 512}))
    assert "result" not in got
    assert "larger than" in got["result_omitted"]


def test_a_failed_action_carries_its_error_and_not_a_result():
    action = Action(name="daq.table", status=ActionStatus.FAILED, result={"partial": True})
    action.error = "no such source"
    got = _frame(action)
    assert got["error"] == "no such source"
    assert "result" not in got


def test_the_summary_fields_are_unchanged():
    got = _frame(_completed(result={"ok": True}))
    assert got["name"] == "daq.table" and got["status"] == "completed"
