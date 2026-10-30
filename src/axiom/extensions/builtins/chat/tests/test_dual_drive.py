# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Dual drive: the web app's screen reaches the prompt, and the conversation can change it.

The contract mirrors appkit's ``chat/viewContext.ts``. What is held here:

- the view context is parsed, bounded and validated, and the focused graph survives the cut;
- it reaches the system prompt and never the session's messages or the stored transcript;
- the ``ui_*`` tools are offered only on a turn whose view accepts them, and a call becomes a
  ``ui_command`` frame;
- the planner turns a clear request into a command when the model will not call a tool, and
  resolves series names against the site's channel list;
- a question about the focused chart reads that chart's series first, counts as a measurement,
  and the answer says where the number came from;
- a site node's answer is asked for whole, so the verbs it ran come back with it.
"""

from __future__ import annotations

import json
import os
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from axiom.extensions.builtins.chat import bound_tools as bt
from axiom.extensions.builtins.chat import view_drive as vd
from axiom.extensions.builtins.chat.headless import HeadlessChat
from axiom.extensions.builtins.chat.providers.sse_render import SseRenderProvider
from axiom.infra.bus import EventBus
from axiom.infra.gateway import CompletionResponse, Gateway, ToolUseBlock

ALL = list(vd.ACTIONS)


def _view(**over):
    view = {
        "schema": vd.SCHEMA,
        "app": "web",
        "route": "#/charts",
        "screen": "charts",
        "site": "north-loop",
        "binding": {"mode": "bound", "site": "north-loop", "node": "node.example.org"},
        "groups": [{"id": "k1", "graphs": [{
            "key": "k1/g1", "site": "north-loop", "series": ["inst:Flow"],
            "window": {"from": "2026-03-02T00:00:00Z", "to": "2026-03-03T00:00:00Z"},
        }]}],
        "focus": "k1/g1",
        "actions": ALL,
        "views": ["charts", "chat"],
        "summary": "One chart of Flow.",
    }
    view.update(over)
    return vd.parse_view_context(view)


CATALOGUE = [
    {"feed": "inst", "channel": "Flow", "first_ts": "2026-01-01T00:00:00Z", "last_ts": "2026-04-01T00:00:00Z"},
    {"feed": "console", "channel": "flow", "first_ts": "2025-01-01T00:00:00Z", "last_ts": "2025-02-01T00:00:00Z"},
    {"feed": "model.state", "channel": "est_flow", "first_ts": "2026-01-01T00:00:00Z", "last_ts": "2026-04-01T00:00:00Z"},
    {"feed": "inst", "channel": "WallTemp1", "first_ts": "2026-01-01T00:00:00Z", "last_ts": "2026-04-01T00:00:00Z"},
    {"feed": "inst", "channel": "WallTemp2", "first_ts": "2026-01-01T00:00:00Z", "last_ts": "2026-04-01T00:00:00Z"},
    {"feed": "model.state", "channel": "avg_wall_temp", "first_ts": "2026-01-01T00:00:00Z", "last_ts": "2026-04-01T00:00:00Z"},
]


# ---- the contract ----------------------------------------------------------------------------


class TestTheContract:
    def test_a_typed_context_keeps_its_structure(self):
        v = _view()
        assert v["focus"] == "k1/g1" and v["actions"] == ALL
        assert vd.focused_graph(v)["series"] == ["inst:Flow"]

    def test_unknown_actions_are_dropped(self):
        assert _view(actions=["create_chart", "format_disk"])["actions"] == ["create_chart"]

    def test_the_older_shapes_are_summary_only(self):
        legacy = vd.parse_view_context({"surface": "chart", "summary": "a chart", "label": "x"})
        assert legacy["summary"] == "a chart" and legacy["actions"] == []
        assert vd.parse_view_context("on screen: a chart")["actions"] == []
        assert vd.parse_view_context("") is None and vd.parse_view_context(None) is None

    def test_it_is_bounded_and_the_focused_graph_survives(self):
        graphs = [{"key": f"k1/g{i}", "site": "s", "series": [f"f:c{j}" for j in range(40)],
                   "drawn": {"notes": ["n" * 200] * 16}} for i in range(60)]
        v = _view(groups=[{"id": "k1", "graphs": graphs}], focus="k1/g59", summary="x" * 9000)
        assert v["truncated"] is True
        assert len(json.dumps(v)) <= vd.LIMITS["bytes"]
        assert vd.graph(v, "k1/g59") is not None
        assert all(len(g["series"]) <= vd.LIMITS["series"] for g in vd.graphs_of(v))

    @pytest.mark.parametrize("raw, why", [
        ({"type": "update_chart", "graph": "k9/g9", "add": ["a:b"]}, "no chart"),
        ({"type": "create_chart"}, "at least one series"),
        ({"type": "set_window", "window": {"from": "2026-03-03T00:00:00Z", "to": "2026-03-02T00:00:00Z"}},
         "end after"),
        ({"type": "set_window", "window": {"from": "yesterday"}}, "both ends"),
        ({"type": "open_view", "view": "billing"}, "no view"),
        ({"type": "update_chart", "graph": "k1/g1"}, "change something"),
        ({"type": "explode"}, "unknown action"),
    ])
    def test_a_command_the_view_cannot_take_is_refused_with_a_reason(self, raw, why):
        cmd, reason = vd.validate_command(raw, _view())
        assert cmd is None and why in reason

    def test_set_window_defaults_to_the_focused_graph(self):
        cmd, _ = vd.validate_command({"type": "set_window", "window": {
            "from": "2026-03-02T06:00:00Z", "to": "2026-03-02T12:00:00Z"}}, _view())
        assert cmd["graph"] == "k1/g1" and cmd["id"].startswith("uc_") and cmd["auto"] is True

    def test_a_screen_that_does_not_accept_an_action_refuses_it(self):
        cmd, reason = vd.validate_command({"type": "remove_chart", "graph": "k1/g1"},
                                          _view(actions=["focus"]))
        assert cmd is None and "does not accept" in reason


# ---- the planner ------------------------------------------------------------------------------


class TestThePlanner:
    def test_plot_a_series_for_a_day_creates_a_chart_with_that_window(self):
        plan = vd.plan_ui_commands("Plot north loop flow for 2026-03-05", _view(), CATALOGUE)
        (cmd,) = plan.commands
        assert cmd["type"] == "create_chart" and cmd["source"] == "planner"
        # coverage wins over the stale console channel; the measured feed over the model one
        assert cmd["series"] == ["inst:Flow"]
        assert cmd["window"] == {"from": "2026-03-05T00:00:00Z", "to": "2026-03-06T00:00:00Z"}
        assert cmd["site"] == "north-loop"
        assert "applying this change" in plan.note

    def test_add_to_that_chart_updates_the_focused_one_with_every_equal_match(self):
        plan = vd.plan_ui_commands("Add wall temperature to that chart", _view(), CATALOGUE)
        (cmd,) = plan.commands
        assert cmd["type"] == "update_chart" and cmd["graph"] == "k1/g1"
        assert cmd["add"] == ["inst:WallTemp1", "inst:WallTemp2"]

    def test_a_name_nothing_matches_is_said_not_guessed(self):
        plan = vd.plan_ui_commands("Plot banana pressure", _view(), CATALOGUE)
        assert plan.commands == [] and plan.missing == ["banana pressure"]
        assert "could not find" in plan.note

    def test_a_question_is_not_a_command(self):
        assert vd.plan_ui_commands("What's the peak in the focused chart?", _view(), CATALOGUE).commands == []
        assert vd.plan_ui_commands("show me what happened", _view(), CATALOGUE).note == ""

    def test_remove_set_window_open_and_focus(self):
        v = _view()
        assert vd.plan_ui_commands("remove this chart", v, CATALOGUE).commands[0]["type"] == "remove_chart"
        cmd = vd.plan_ui_commands("remove flow from the chart", v, CATALOGUE).commands[0]
        assert cmd["type"] == "update_chart" and cmd["remove"] == ["inst:Flow"]
        cmd = vd.plan_ui_commands("zoom to 2026-03-02", v, CATALOGUE).commands[0]
        assert cmd["type"] == "set_window" and cmd["window"]["from"] == "2026-03-02T00:00:00Z"
        assert vd.plan_ui_commands("open the chat view", v, CATALOGUE).commands[0]["view"] == "chat"
        assert vd.plan_ui_commands("focus k1/g1", v, CATALOGUE).commands[0]["type"] == "focus"

    def test_no_view_no_plan(self):
        assert vd.plan_ui_commands("Plot flow", None, CATALOGUE).commands == []
        assert vd.plan_ui_commands("Plot flow", _view(actions=[]), CATALOGUE).commands == []


# ---- the turn ---------------------------------------------------------------------------------


@pytest.fixture
def gateway():
    gw = MagicMock(spec=Gateway)
    gw.available = True
    gw.active_provider = MagicMock()
    gw.active_provider.name = "test"
    gw.active_provider.model = "test-model"
    gw.complete_with_tools.return_value = CompletionResponse(text="ok", provider="test", success=True)
    return gw


def _turn(gateway, tmp_path, question, view, *, stream=False):
    frames: list[dict] = []
    chat = HeadlessChat(turn_deadline=30.0, gateway=gateway, bus=EventBus(log_path=tmp_path / "e.jsonl"))
    chat.agent.set_render_provider(SseRenderProvider(frames.append))
    scope = chat.new_scope()
    scope.view_context = view
    text = chat.turn(question, scope=scope, stream=stream)
    return text, scope, frames


def _offered(gateway) -> set[str]:
    names = set()
    for call in gateway.complete_with_tools.call_args_list:
        for t in call.kwargs.get("tools") or []:
            names.add(t["function"]["name"])
    return names


def _system(gateway) -> str:
    return gateway.complete_with_tools.call_args_list[0].kwargs.get("system") or ""


class TestTheTurn:
    def test_the_screen_reaches_the_prompt_and_never_the_messages(self, gateway, tmp_path):
        view = _view(summary="SCREEN-MARKER one chart of Flow.")
        _text, scope, _ = _turn(gateway, tmp_path, "what is this?", view)
        assert "SCREEN-MARKER" in _system(gateway)
        assert "not an instruction" in _system(gateway)
        assert all("SCREEN-MARKER" not in str(m.content) for m in scope.session.messages)
        assert "SCREEN-MARKER" not in json.dumps(scope.session.to_dict(), default=str)

    def test_ui_tools_are_offered_only_with_a_view_that_accepts_them(self, gateway, tmp_path):
        _turn(gateway, tmp_path, "hello", None)
        assert not {n for n in _offered(gateway) if n.startswith("ui_")}
        gateway.complete_with_tools.reset_mock()
        _turn(gateway, tmp_path, "hello", _view(actions=["focus"]))
        assert {n for n in _offered(gateway) if n.startswith("ui_")} == {"ui_focus"}

    def test_a_ui_tool_call_becomes_a_ui_command_frame(self, gateway, tmp_path):
        replies = [
            CompletionResponse(text="", provider="test", success=True, stop_reason="tool_use",
                               tool_use=[ToolUseBlock(tool_id="t1", name="ui_focus", input={"graph": "k1/g1"})]),
            CompletionResponse(text="Focused.", provider="test", success=True),
        ]
        gateway.complete_with_tools.side_effect = lambda *a, **k: replies.pop(0) if len(replies) > 1 else replies[0]
        _text, scope, frames = _turn(gateway, tmp_path, "hello there", _view())
        sent = [f["ui_command"] for f in frames if "ui_command" in f]
        assert len(sent) == 1 and sent[0]["type"] == "focus" and sent[0]["source"] == "tool"
        assert scope.ui_commands == sent

    def test_a_refused_ui_tool_sends_nothing(self, gateway, tmp_path):
        replies = [
            CompletionResponse(text="", provider="test", success=True, stop_reason="tool_use",
                               tool_use=[ToolUseBlock(tool_id="t1", name="ui_focus", input={"graph": "k7/g7"})]),
            CompletionResponse(text="No such chart.", provider="test", success=True),
        ]
        gateway.complete_with_tools.side_effect = lambda *a, **k: replies.pop(0) if len(replies) > 1 else replies[0]
        _text, scope, frames = _turn(gateway, tmp_path, "hello there", _view())
        assert not [f for f in frames if "ui_command" in f] and scope.ui_commands == []

    def test_the_planner_sends_the_command_and_withholds_the_tools(self, gateway, tmp_path, monkeypatch):
        monkeypatch.setattr(vd, "catalogue", lambda: CATALOGUE)
        _text, scope, frames = _turn(gateway, tmp_path, "Plot flow for 2026-03-05", _view())
        sent = [f["ui_command"] for f in frames if "ui_command" in f]
        assert [c["type"] for c in sent] == ["create_chart"] and sent[0]["source"] == "planner"
        assert not {n for n in _offered(gateway) if n.startswith("ui_")}
        assert "applying this change" in _system(gateway)

    def test_a_terminal_turn_is_unchanged(self, gateway, tmp_path):
        _text, scope, frames = _turn(gateway, tmp_path, "Plot flow for 2026-03-05", None)
        assert scope.ui_commands == [] and not [f for f in frames if "ui_command" in f]
        assert "person's screen" not in _system(gateway)


# ---- the focused chart, answered from its own series ----------------------------------------


def _bound(monkeypatch, *, show_rows):
    from axiom.extensions.builtins.chat.tools import ToolDef
    from axiom.infra.orchestrator.actions import ActionCategory

    tool = ToolDef(name="data__show", description="a series", category=ActionCategory.READ,
                   parameters={"type": "object", "properties": {}}, source=bt.TOOL_SOURCE_BOUND)
    found = bt.BoundTools(site="north-loop", face_url="https://node.example.org",
                          tools={"data__show": tool}, skills={"data__show": "data.show"})
    monkeypatch.setattr(bt, "bound_tools", lambda: found)
    calls: list[dict] = []

    def execute(name, params):
        calls.append(dict(params))
        rows = show_rows[params["fn"]]
        return {"ok": True, "value": {"data": {
            "series": rows, "bucket": "30 minutes", "fn": params["fn"], "units": ["L/s"],
            "window": {"start": params.get("start"), "end": params.get("end")}}}}

    monkeypatch.setattr(bt, "execute", execute)
    return calls


class TestTheFocusedChart:
    ROWS = {
        "max": [{"t": "2026-03-02T09:30:00Z", "value": 12.5, "unit": "L/s"},
                {"t": "2026-03-02T10:00:00Z", "value": 4.0, "unit": "L/s"}],
        "min": [{"t": "2026-03-02T01:00:00Z", "value": 0.2, "unit": "L/s"}],
    }

    def test_a_peak_question_reads_the_chart_and_says_where_from(self, gateway, tmp_path, monkeypatch):
        calls = _bound(monkeypatch, show_rows=self.ROWS)
        gateway.complete_with_tools.return_value = CompletionResponse(
            text="The peak was 12.5 L/s at 09:30.", provider="test", success=True)
        text, scope, frames = _turn(gateway, tmp_path, "What's the peak in the focused chart?", _view())
        assert {c["fn"] for c in calls} == {"max"}
        assert calls[0]["target"] == "Flow" and calls[0]["feed"] == "inst"
        assert calls[0]["start"] == "2026-03-02T00:00:00Z"
        assert "peak 12.5 L/s in the bucket starting 2026-03-02T09:30:00Z" in _system(gateway)
        assert text.endswith("30 minutes max buckets.")
        assert "Source: data.show on north-loop (gold), inst:Flow" in text
        assert scope.session.context["turn_data"]["measured"] is True
        assert not text.startswith("(not a measurement")

    def test_the_lowest_is_read_only_when_asked(self, gateway, tmp_path, monkeypatch):
        calls = _bound(monkeypatch, show_rows=self.ROWS)
        _turn(gateway, tmp_path, "What is the range of the focused chart?", _view())
        assert {c["fn"] for c in calls} == {"max", "min"}
        assert "lowest 0.2 L/s" in _system(gateway)

    def test_a_question_about_something_else_reads_nothing(self, gateway, tmp_path, monkeypatch):
        calls = _bound(monkeypatch, show_rows=self.ROWS)
        _turn(gateway, tmp_path, "hello", _view())
        assert calls == []


# ---- a node's answer, asked for whole ---------------------------------------------------------


class TestANodeAnswersWhole:
    def test_a_remote_node_provider_is_not_streamed_so_its_verbs_count(self, gateway, tmp_path, monkeypatch):
        _bound(monkeypatch, show_rows=TestTheFocusedChart.ROWS)
        gateway.preview_provider.return_value = SimpleNamespace(placement="remote-node", context_window=0,
                                                                 model="node-model")
        gateway.complete_with_tools.return_value = CompletionResponse(
            text="The newest reading is 3 L/s.", provider="node", success=True,
            served_verbs=["status_at"])
        text, scope, frames = _turn(gateway, tmp_path, "what is the latest value?", None, stream=True)
        gateway.stream_with_tools.assert_not_called()
        assert scope.session.context["turn_data"]["measured"] is True
        assert not text.startswith("(not a measurement")
        assert any(f.get("chunk") == "The newest reading is 3 L/s." for f in frames)

    def test_another_provider_still_streams(self, gateway, tmp_path):
        gateway.preview_provider.return_value = SimpleNamespace(placement="local", context_window=0)
        chat = HeadlessChat(turn_deadline=30.0, gateway=gateway, bus=EventBus(log_path=tmp_path / "e.jsonl"))
        assert chat.agent._answers_whole() is False


# ---- a bound data question that names a channel ----------------------------------------------


def _bound_latest(monkeypatch):
    from axiom.extensions.builtins.chat.tools import ToolDef
    from axiom.infra.orchestrator.actions import ActionCategory

    def tool(name):
        return ToolDef(name=name, description=name, category=ActionCategory.READ,
                       parameters={"type": "object", "properties": {}}, source=bt.TOOL_SOURCE_BOUND)

    found = bt.BoundTools(site="north-loop", face_url="https://node.example.org",
                          tools={n: tool(n) for n in ("data__show", "data__latest", "data__channels")})
    monkeypatch.setattr(bt, "bound_tools", lambda: found)
    monkeypatch.setattr(vd, "catalogue", lambda: CATALOGUE)
    calls: list[tuple[str, dict]] = []

    def execute(name, params):
        calls.append((name, dict(params)))
        if name == "data__latest":
            return {"ok": True, "value": {"data": {"readings": [
                {"feed": "inst", "channel": params["channel"], "unit": "degC",
                 "ts": "2026-03-02T17:00:40Z", "value": 27.8}]}}}
        return {"ok": True, "value": {"data": {
            "series": [{"t": "2026-03-01T14:00:00Z", "value": 41.5, "unit": "degC"}],
            "bucket": "30 minutes", "window": {"start": params.get("start"), "end": params.get("end")}}}}

    monkeypatch.setattr(bt, "execute", execute)
    return calls


class TestANamedChannel:
    def test_latest_reads_the_channel_the_question_names(self, gateway, tmp_path, monkeypatch):
        calls = _bound_latest(monkeypatch)
        gateway.complete_with_tools.return_value = CompletionResponse(
            text="WallTemp1 reads 27.8 degC.", provider="node", success=True)
        text, scope, _ = _turn(gateway, tmp_path, "What is the latest WallTemp1 reading?", None)
        assert calls == [("data__latest", {"channel": "WallTemp1"})]
        assert "inst:WallTemp1: 27.8 degC at 2026-03-02T17:00:40Z" in _system(gateway)
        assert text.endswith("Source: data.latest on north-loop (gold), inst:WallTemp1.")
        assert scope.session.context["turn_data"]["measured"] is True

    def test_a_peak_on_a_day_reads_that_day(self, gateway, tmp_path, monkeypatch):
        calls = _bound_latest(monkeypatch)
        gateway.complete_with_tools.return_value = CompletionResponse(
            text="It peaked at 41.5 degC.", provider="node", success=True)
        text, _scope, _ = _turn(gateway, tmp_path, "What was the peak WallTemp2 temperature on 2026-03-01?", None)
        assert calls == [("data__show", {"target": "WallTemp2", "feed": "inst", "fn": "max",
                                         "start": "2026-03-01T00:00:00Z", "end": "2026-03-02T00:00:00Z"})]
        assert "Source: data.show on north-loop (gold), inst:WallTemp2" in text

    def test_a_rate_limited_read_is_tried_once_more(self, gateway, tmp_path, monkeypatch):
        calls = _bound_latest(monkeypatch)
        from axiom.extensions.builtins.chat import agent as agent_mod

        monkeypatch.setattr(agent_mod, "PRECALL_RETRY_S", 0)
        good = bt.execute
        refused = {"n": 0}

        def flaky(name, params):
            if refused["n"] == 0:
                refused["n"] += 1
                return {"ok": False, "value": None, "errors": ["refused by a rate limit (0.2/s, burst 3)"]}
            return good(name, params)

        monkeypatch.setattr(bt, "execute", flaky)
        gateway.complete_with_tools.return_value = CompletionResponse(text="", provider="node", success=True)
        text, _scope, _ = _turn(gateway, tmp_path, "What is the latest WallTemp1 reading?", None)
        assert text.startswith("Read just now: inst:WallTemp1: 27.8 degC")
        assert len(calls) == 1  # the second, successful read

    def test_a_question_naming_no_channel_reads_nothing(self, gateway, tmp_path, monkeypatch):
        calls = _bound_latest(monkeypatch)
        _turn(gateway, tmp_path, "What is the latest banana reading?", None)
        assert calls == []

    def test_an_empty_answer_from_the_node_is_said(self, gateway, tmp_path, monkeypatch):
        _bound_latest(monkeypatch)
        gateway.complete_with_tools.return_value = CompletionResponse(text="", provider="node", success=True)
        text, _scope, _ = _turn(gateway, tmp_path, "how are things?", None)
        assert text.startswith("The node for north-loop returned no answer")

    def test_an_answer_that_ignores_the_read_is_led_by_it(self, gateway, tmp_path, monkeypatch):
        _bound_latest(monkeypatch)
        gateway.complete_with_tools.return_value = CompletionResponse(
            text="I don't have a tool-verified value for that.", provider="node", success=True)
        text, _scope, _ = _turn(gateway, tmp_path, "What is the latest WallTemp1 reading?", None)
        assert text.startswith("Read just now: inst:WallTemp1: 27.8 degC")
        assert "I don't have a tool-verified value" in text

    def test_an_empty_answer_after_a_read_is_answered_from_the_read(self, gateway, tmp_path, monkeypatch):
        _bound_latest(monkeypatch)
        gateway.complete_with_tools.return_value = CompletionResponse(text="", provider="node", success=True)
        text, _scope, _ = _turn(gateway, tmp_path, "What is the latest WallTemp1 reading?", None)
        assert text.startswith("Read just now: inst:WallTemp1: 27.8 degC at 2026-03-02T17:00:40Z.")
        assert text.endswith("Source: data.latest on north-loop (gold), inst:WallTemp1.")

    def test_a_read_the_node_refused_does_not_make_a_measurement(self, gateway, tmp_path, monkeypatch):
        _bound_latest(monkeypatch)
        from axiom.extensions.builtins.chat import agent as agent_mod

        monkeypatch.setattr(agent_mod, "PRECALL_RETRY_S", 0)
        monkeypatch.setattr(bt, "execute", lambda name, params: {
            "ok": False, "value": None, "errors": ["refused: rate limit: 0.2/s (burst 3) for anonymous callers"]})
        gateway.complete_with_tools.return_value = CompletionResponse(
            text="The read was refused.", provider="node", success=True)
        text, scope, _ = _turn(gateway, tmp_path, "What is the latest WallTemp1 reading?", None)
        assert scope.session.context["turn_data"]["measured"] is False
        assert text.startswith(f"({bt.NOT_A_MEASUREMENT})")


# ---- the HTTP surface -------------------------------------------------------------------------


def test_the_send_route_hands_the_view_to_the_turn_and_stores_only_the_words(monkeypatch):
    from fastapi import APIRouter, FastAPI
    from fastapi.testclient import TestClient
    from sqlalchemy import create_engine
    from sqlalchemy.pool import StaticPool

    from axiom.extensions.builtins.chat import api
    from axiom.extensions.builtins.chat.db_models import Base

    monkeypatch.delenv("AXIOM_SERVED_SITES", raising=False)
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool, future=True)
    Base.metadata.create_all(engine)
    seen: dict = {}

    def runner(session, content, render, *, tenant_id=None):
        seen["view"] = vd.parse_view_context(vd.REQUEST_VIEW.get())
        session.add_message("user", content)
        render.stream_text(iter([SimpleNamespace(type="text", text="ok")]))
        session.add_message("assistant", "ok")
        return "ok"

    api.set_test_engine(engine)
    api.set_turn_runner(runner)
    try:
        router = APIRouter(prefix="/api/v1")
        api.register_routes(router, subpath="/chat")
        app = FastAPI()
        app.include_router(router)
        client = TestClient(app)
        cid = client.post("/api/v1/chat/conversations", json={}).json()["id"]
        raw = {"schema": vd.SCHEMA, "app": "web", "route": "#/charts", "screen": "charts",
               "actions": ["focus"], "summary": "SCREEN-MARKER", "groups": []}
        r = client.post(f"/api/v1/chat/conversations/{cid}/messages?stream=true",
                        json={"content": "hi", "view_context": raw})
        assert r.status_code == 200
        assert seen["view"]["summary"] == "SCREEN-MARKER" and seen["view"]["actions"] == ["focus"]
        detail = client.get(f"/api/v1/chat/conversations/{cid}").json()
        assert "SCREEN-MARKER" not in json.dumps(detail)
        assert [m["content"] for m in detail["messages"]] == ["hi", "ok"]
    finally:
        api.reset_turn_runner()
        api.set_test_engine(None)
        engine.dispose()


# ---- against a real node ----------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.skipif(os.environ.get("AXIOM_E2E_BOUND_NODE") != "1",
                    reason="needs a bound site node (AXIOM_E2E_BOUND_NODE=1 and a current binding)")
def test_the_three_prompts_against_the_bound_node():
    """Plot a series for a day, add one to that chart, ask for the focused chart's peak."""
    from axiom.extensions.builtins.chat.api import _default_turn_runner
    from axiom.infra.orchestrator.session import Session

    day = os.environ.get("AXIOM_E2E_DAY", "2026-10-05")
    plot = os.environ.get("AXIOM_E2E_PLOT", "power")
    add = os.environ.get("AXIOM_E2E_ADD", "fuel temperature")
    mode = vd.catalogue() and bt.bound_tools()
    assert mode, "not bound, or the node lists no channels"
    site = bt.bound_tools().site

    def run(question, view):
        frames: list[dict] = []
        token = vd.REQUEST_VIEW.set(view)
        try:
            text = _default_turn_runner(Session(), question, SseRenderProvider(frames.append))
        finally:
            vd.REQUEST_VIEW.reset(token)
        return text, [f["ui_command"] for f in frames if "ui_command" in f]

    base = {"schema": vd.SCHEMA, "app": "web", "route": "#/charts", "screen": "charts", "site": site,
            "actions": ALL, "groups": [{"id": "k1", "graphs": []}]}
    _text, sent = run(f"Plot {plot} for {day}", base)
    assert sent and sent[0]["type"] == "create_chart" and sent[0]["series"]
    created = {"key": "k1/g1", "site": site, "series": sent[0]["series"], "window": sent[0].get("window")}
    view = {**base, "groups": [{"id": "k1", "graphs": [created]}], "focus": "k1/g1"}
    _text, sent = run(f"Add {add} to that chart", view)
    assert sent and sent[0]["type"] == "update_chart" and sent[0]["add"]
    created["series"] = created["series"] + sent[0]["add"]
    text, _ = run("What's the peak in the focused chart?", view)
    assert "Source: data.show on" in text


def test_a_silent_turn_keeps_the_stream_alive(monkeypatch):
    """A node that answers in one block after its own tools is silent for a while; the
    stream says it is alive so a browser's idle timeout does not abandon the turn."""
    import time

    from fastapi import APIRouter, FastAPI
    from fastapi.testclient import TestClient
    from sqlalchemy import create_engine
    from sqlalchemy.pool import StaticPool

    from axiom.extensions.builtins.chat import api
    from axiom.extensions.builtins.chat.db_models import Base

    monkeypatch.delenv("AXIOM_SERVED_SITES", raising=False)
    monkeypatch.setattr(api, "KEEPALIVE_S", 0.05)
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool, future=True)
    Base.metadata.create_all(engine)
    api.set_test_engine(engine)

    def slow(session, content, render, *, tenant_id=None):
        session.add_message("user", content)
        time.sleep(0.3)
        render.render_message("assistant", "done")
        session.add_message("assistant", "done")
        return "done"

    api.set_turn_runner(slow)
    try:
        router = APIRouter(prefix="/api/v1")
        api.register_routes(router, subpath="/chat")
        app = FastAPI()
        app.include_router(router)
        client = TestClient(app)
        cid = client.post("/api/v1/chat/conversations", json={}).json()["id"]
        body = client.post(f"/api/v1/chat/conversations/{cid}/messages?stream=true",
                           json={"content": "hi"}).text
    finally:
        api.reset_turn_runner()
        api.set_test_engine(None)
        engine.dispose()
    assert ": keepalive" in body
    assert '"chunk": "done"' in body


# ---- chart kinds and controls (ChartSpec 2.0, ADR-214) ------------------------------------------


def _view_with_controls():
    view = _view()
    g = view["groups"][0]["graphs"][0]
    raw = {**g, "kind": "heatmap", "controls": [
        {"id": "z", "type": "slider", "label": "Axial plane", "value": 0, "options": [{"value": 0}, {"value": 10}]},
        {"id": "modes", "type": "toggle", "label": "Mode lines", "value": False}]}
    return vd.parse_view_context({**_view(), "groups": [{"id": "k1", "graphs": [raw]}]} | {
        "schema": vd.SCHEMA, "app": "web", "route": "#/charts", "screen": "charts", "actions": list(vd.ACTIONS)})


class TestKindsAndControls:
    def test_the_screen_tells_the_model_each_chart_kind_and_control(self):
        prompt = vd.render_prompt(_view_with_controls())
        assert "kind heatmap" in prompt and "control z (slider 'Axial plane') = 0 of [0, 10]" in prompt

    def test_set_control_moves_a_control_the_chart_has(self):
        cmd, why = vd.validate_command({"type": "set_control", "control": "z", "value": 10}, _view_with_controls())
        assert cmd and cmd["graph"] == "k1/g1" and cmd["value"] == 10, why

    @pytest.mark.parametrize("raw, why", [
        ({"type": "set_control", "control": "q", "value": 1}, "no control 'q'"),
        ({"type": "set_control", "control": "z", "value": 15}, "cannot take 15"),
        ({"type": "set_control", "control": "modes", "value": "on"}, "true or false"),
        ({"type": "set_control", "control": "z"}, "needs a value"),
    ])
    def test_a_control_value_it_cannot_take_is_refused(self, raw, why):
        cmd, reason = vd.validate_command(raw, _view_with_controls())
        assert cmd is None and why in reason

    def test_a_chart_kind_is_checked_against_the_registry(self):
        pytest.importorskip("axiom.extensions.builtins.scidisplay.unified_spec")
        ok, _ = vd.validate_command({"type": "create_chart", "series": ["inst:Flow"], "kind": "pie"}, _view())
        assert ok and ok["kind"] == "pie"
        bad, why = vd.validate_command({"type": "update_chart", "graph": "k1/g1", "kind": "radar"}, _view())
        assert bad is None and "no chart kind" in why

    def test_a_whole_spec_can_create_a_chart(self):
        pytest.importorskip("axiom.extensions.builtins.scidisplay.unified_spec")
        spec = {"schema_version": "2.0", "kind": "gauge", "title": "Load",
                "data": {"value": {"value": 3, "max": 5}}}
        cmd, why = vd.validate_command({"type": "create_chart", "spec": spec}, _view())
        assert cmd and cmd["spec"]["kind"] == "gauge", why
        bad, why = vd.validate_command({"type": "create_chart", "spec": {"kind": "gauge"}}, _view())
        assert bad is None and "not valid" in why


def _flame_view():
    raw = {"key": "k1/g1", "site": "north-loop", "series": [], "kind": "flame", "controls": [
        {"id": "zoom", "type": "path", "label": "Zoom", "value": ""},
        {"id": "find", "type": "text", "label": "Search", "value": ""}]}
    return vd.parse_view_context({"schema": vd.SCHEMA, "app": "web", "route": "#/charts", "screen": "charts",
                                  "groups": [{"id": "k1", "graphs": [raw]}], "focus": "k1/g1",
                                  "actions": list(vd.ACTIONS)})


class TestFramePaths:
    def test_focus_with_a_path_zooms_the_frame(self):
        cmd, why = vd.validate_command({"type": "focus", "graph": "k1/g1", "path": "main;draw"}, _flame_view())
        assert cmd and cmd["path"] == ["main", "draw"] and cmd["control"] == "zoom", why

    def test_a_path_on_a_chart_without_frames_is_refused(self):
        cmd, why = vd.validate_command({"type": "focus", "graph": "k1/g1", "path": "a"}, _view())
        assert cmd is None and "no frames" in why

    def test_search_is_a_set_control(self):
        cmd, why = vd.validate_command({"type": "set_control", "control": "find", "value": "parse"}, _flame_view())
        assert cmd and cmd["value"] == "parse", why


def _scene_view():
    raw = {"key": "k1/g1", "site": "north-loop", "series": [], "kind": "mesh3d", "controls": [
        {"id": "camera", "type": "segmented", "label": "View", "value": "iso", "options": [{"value": "iso"}, {"value": "top"}]},
        {"id": "pick", "type": "text", "label": "Picked", "value": ""}]}
    return vd.parse_view_context({"schema": vd.SCHEMA, "app": "web", "route": "#/charts", "screen": "charts",
                                  "groups": [{"id": "k1", "graphs": [raw]}], "focus": "k1/g1",
                                  "actions": list(vd.ACTIONS)})


class TestTheCamera:
    def test_set_camera_by_view_or_angles(self):
        cmd, why = vd.validate_command({"type": "set_camera", "camera": "top"}, _scene_view())
        assert cmd and cmd["camera"] == "top" and cmd["graph"] == "k1/g1", why
        cmd, why = vd.validate_command({"type": "set_camera", "camera": {"azimuth": 30, "elevation": 20}},
                                       _scene_view())
        assert cmd and cmd["camera"] == {"azimuth": 30.0, "elevation": 20.0}, why

    @pytest.mark.parametrize("camera, why", [("bottom", "no camera view"), ({"azimuth": 1}, "needs an azimuth"),
                                              (3, "needs a camera")])
    def test_a_camera_it_cannot_take_is_refused(self, camera, why):
        cmd, reason = vd.validate_command({"type": "set_camera", "camera": camera}, _scene_view())
        assert cmd is None and why in reason

    def test_a_flat_chart_has_no_camera(self):
        cmd, reason = vd.validate_command({"type": "set_camera", "camera": "top"}, _view())
        assert cmd is None and "not a 3D scene" in reason

    def test_focus_picks_an_element(self):
        cmd, why = vd.validate_command({"type": "focus", "graph": "k1/g1", "element": "D-06"}, _scene_view())
        assert cmd and cmd["element"] == "D-06" and cmd["control"] == "pick", why
