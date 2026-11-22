# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""A chart question that names a channel reads that channel's series before the model answers.

Seen live with a small local model: asked to chart a named channel for a day, it never called the
series read (a generic gold read that takes a table, a column, a bucket and a filter), wrote a
made-up result instead, and no chart was drawn. A data turn already reads a named channel itself
(``view_drive.named_calls``), but only as a summary (``data.show``), and a dotted channel name such
as ``temperature.inlet`` never matched, because the question was split into words at the dot.
The chart the answer carries is built from the turn's own reads (``charts.answer``), so reading
the series here draws the chart whatever the model does.
"""

from __future__ import annotations

from axiom.extensions.builtins.chat import view_drive

CATALOGUE = [
    {"feed": "example.loop-sample", "channel": "temperature.inlet", "unit": "degC"},
    {"feed": "example.loop-sample", "channel": "flow.primary", "unit": "L/min"},
]
TOOLS = {"data__series": object(), "data__show": object(), "data__latest": object()}


def test_a_chart_question_naming_a_dotted_channel_reads_its_series_for_the_day():
    calls = view_drive.named_calls("Chart temperature.inlet on 2026-11-12.", CATALOGUE, TOOLS)
    assert [c["tool"] for c in calls] == ["data__series"]
    params = calls[0]["params"]
    assert "channel = 'temperature.inlet'" in params["filter"]
    assert "feed = 'example.loop-sample'" in params["filter"]
    assert params["window"]["start"].startswith("2026-11-12")
    assert params["window"]["end"].startswith("2026-11-13")
    assert params["group_by"] == ["feed", "channel"]


def test_a_read_answered_on_this_node_reaches_the_model_with_its_times():
    """A gold read answered in this process carries ``datetime`` values (a bound node's arrive as
    JSON text). Writing the result into the conversation raised ``TypeError`` and ended the turn
    with no answer and no chart."""
    import json
    from datetime import UTC, datetime

    from axiom.extensions.builtins.chat.agent import _bounded_result_json

    result = {"ok": True, "value": {"served": {"covered": {"start": datetime(2026, 11, 12, tzinfo=UTC)}}}}
    assert "2026-11-12" in json.loads(_bounded_result_json(result))["value"]["served"]["covered"]["start"]


def test_a_turn_that_runs_out_of_time_still_carries_the_chart_of_what_it_read():
    """A small local model on a busy machine can take longer than the web turn's deadline. The
    series was already read, before the model was asked; the chart is drawn from that read, not
    from the model, so it is shown with the time-limit notice instead of being dropped. Seen live:
    the page showed "Thi", no chart and no notice."""
    from unittest.mock import MagicMock

    from axiom.extensions.builtins.chat.agent import ChatAgent
    from axiom.extensions.builtins.chat.providers.sse_render import SseRenderProvider
    from axiom.infra.bus import EventBus
    from axiom.infra.gateway import Gateway
    from axiom.infra.orchestrator.session import Session

    agent = ChatAgent(gateway=MagicMock(spec=Gateway), bus=EventBus(), session=Session())
    frames: list[dict] = []
    agent.set_render_provider(SseRenderProvider(frames.append))
    points = [{"t": f"2026-11-12T{h:02d}:00:00Z", "value": 20.0 + h / 10} for h in range(24)]
    result = {"ok": True, "value": {"data": {"series": [
        {"key": {"feed": "example.loop-sample", "channel": "temperature.inlet"}, "points": points}]}}}
    from axiom.extensions.builtins.chat.agent import _active_scope

    scope = _active_scope(agent, None)
    args = {"filter": "channel = 'temperature.inlet'"}
    agent._collect_chart_read("data__series", args, result, scope=scope)
    from axiom.extensions.builtins.chat.agent import _note_data_read

    scope.session.context["turn_data"] = {"data_tools": ["data__series"]}
    _note_data_read(scope, "data__series", args)
    agent._out_of_time(None, scope=scope, question="Chart temperature.inlet on 2026-11-12")
    assert any("chart_suggestions" in f for f in frames)
    # Its sources too: the read happened, whatever the model managed.
    assert any("citations" in f for f in frames)
    # The page shows only what is rendered: the notice must be, or a cut-off answer reads as whole.
    import json

    from axiom.extensions.builtins.chat.agent import DEADLINE_MESSAGE

    assert DEADLINE_MESSAGE in json.dumps(frames)


def test_a_channel_named_in_plain_words_is_read_too():
    """"Plot the inlet temperature" names ``temperature.inlet`` as a person says it. Seen live: the
    model then built the series read itself and got its arguments wrong three times."""
    catalogue = CATALOGUE + [{"feed": "example.loop-sample", "channel": "temperature.outlet"}]
    calls = view_drive.named_calls(
        "Can you visualize the inlet and outlet temperatures on 2026-11-12?", catalogue, TOOLS)
    assert [c["tool"] for c in calls] == ["data__series"]
    assert "channel IN ('temperature.inlet', 'temperature.outlet')" in calls[0]["params"]["filter"]
    assert view_drive.named_calls("Plot the flow temperature on 2026-11-12", catalogue, TOOLS) == []


def test_once_the_series_is_read_the_model_only_describes_it_with_no_tools(monkeypatch):
    """The chart is drawn from the series this turn already read. Offering about a hundred tools
    (13k of a 15k-token prompt) to describe it made a small model slow past the deadline and let it
    call the same read again with wrong arguments, then narrate the failure. The answer round gets
    no tools, and is asked for one to three plain sentences."""
    from unittest.mock import MagicMock

    from axiom.extensions.builtins.chat import bound_tools
    from axiom.extensions.builtins.chat.agent import ChatAgent
    from axiom.infra.bus import EventBus
    from axiom.infra.gateway import CompletionResponse, Gateway
    from axiom.infra.orchestrator.session import Session

    points = [{"t": f"2026-11-12T{m // 60:02d}:{m % 60:02d}:00Z", "value": 20.0 + m / 1000}
              for m in range(0, 1440, 5)]  # a day at five minutes, as the live read returned
    series = {"ok": True, "value": {"data": {"series": [
        {"key": {"feed": "example.loop-sample", "channel": "temperature.inlet"}, "points": points}]}}}
    monkeypatch.setattr(bound_tools, "data_turn", lambda q, tools: bound_tools.DataTurn(
        site="example-loop", data_tools=frozenset({"data__series"}), is_data=True, local=True))
    monkeypatch.setattr(view_drive, "catalogue", lambda: CATALOGUE)

    agent = ChatAgent(gateway=MagicMock(spec=Gateway), bus=EventBus(), session=Session())

    def precall(planned, all_tools, messages, *, scope):
        results = [(b.tool_id, b.name, series) for b in planned.tool_use]
        agent._record_tool_round(planned, results, messages, scope=scope)
        return results

    monkeypatch.setattr(agent, "_precall_reads", precall)
    asked: list = []

    def answer(messages, system, tools, *a, **k):
        asked.append((system, tools, messages))
        return CompletionResponse(text="It rose from 20.0 to 22.3 degC.", provider="t", success=True)

    monkeypatch.setattr(agent, "_non_streaming_turn", answer)
    monkeypatch.setattr("axiom.extensions.builtins.chat.agent.get_all_tools",
                        lambda: {name: MagicMock(name=name) for name in TOOLS})
    agent.turn("Chart temperature.inlet on 2026-11-12.", stream=False)
    assert asked and asked[0][1] is None
    assert "one to three" in asked[0][0]
    # The numbers are in the prompt already; the full rows (288 points, about 8k tokens) were the
    # bulk of what a small model had to read before its first word, and it ran past the deadline.
    tool_text = [str(m.get("content")) for m in asked[0][2] if m.get("role") == "tool"]
    assert tool_text and all(len(t) < 2000 for t in tool_text), [len(t) for t in tool_text]


def test_the_model_is_given_the_shape_of_what_was_read_and_told_the_chart_is_shown():
    """Seen live: given only min, max and mean, a small model invented a shape ("fell off
    sharply"), compared two ranges wrongly, and said it could not display images and named a tool.
    The read's start, end and when its high and low came are facts to describe from; the chart
    being on the page is a fact too."""
    points = [{"t": "2026-11-12T00:00:00Z", "value": 20.0}, {"t": "2026-11-12T06:00:00Z", "value": 23.0},
              {"t": "2026-11-12T18:00:00Z", "value": 17.0}, {"t": "2026-11-12T23:55:00Z", "value": 19.5}]
    result = {"ok": True, "value": {"data": {"series": [
        {"key": {"feed": "example.loop-sample", "channel": "temperature.inlet"}, "points": points}]}}}
    call = {"tool": "data__series", "params": {}}
    summary = view_drive.summarize_named([call], [("t1", "data__series", result)], "example-loop")
    for fact in ("started at 20", "ended at 19.5", "high 23 at 06:00", "low 17 at 18:00"):
        assert fact in summary.prompt, fact
    assert "already shown" in summary.prompt and "never say you cannot show" in summary.prompt
