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
