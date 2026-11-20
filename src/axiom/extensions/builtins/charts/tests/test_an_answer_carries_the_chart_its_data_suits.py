# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""charts.suggest / charts.render on every surface, and the answer's one chart block (ADR-216)."""

from __future__ import annotations

import json
import logging
import math
import time
from typing import Any

import pytest

from axiom.extensions.builtins.charts import answer, block
from axiom.extensions.builtins.charts.skills import bind
from axiom.infra.skills import SkillContext, SkillRegistry

T = [f"2026-04-{1 + i // 24:02d}T{i % 24:02d}:00:00Z" for i in range(168)]


def _series_envelope(name: str, unit: str, base: float) -> dict[str, Any]:
    """A served data.series result, as the data door returns it."""
    return {"value": {
        "data": {"table": "gold.readings", "column": "value", "bucket": "1 hour", "fn": "mean",
                 "series": [{"t": t, "value": base + 5 * math.sin(i / 6)} for i, t in enumerate(T)]},
        "unit": {"value": unit}, "rows": len(T), "source": {"tier": "gold"},
    }}


def _ctx(reg: SkillRegistry) -> SkillContext:
    return SkillContext(registry=reg, state_dir=None, logger=logging.getLogger("t"))  # type: ignore[arg-type]


@pytest.fixture
def reg() -> SkillRegistry:
    r = SkillRegistry()
    bind(r)
    return r


def test_both_capabilities_declare_their_surfaces(reg: SkillRegistry) -> None:
    s = reg.spec("charts.suggest")
    assert s is not None and {"cli", "mcp", "agent_tool"} <= set(s.surfaces or ())
    assert s.side_effects is False and s.idempotent is True
    r = reg.spec("charts.render")
    assert r is not None and "mcp" in (r.surfaces or ())


def test_the_skill_suggests_and_draws_the_first(reg: SkillRegistry) -> None:
    result = {"series": [{"name": "a", "unit": "degC", "x": T, "y": [float(i % 30) for i in range(168)]}]}
    got = reg.spec("charts.suggest").fn({"result": json.dumps(result), "question": "how did a behave"}, None)
    assert got.ok
    v = got.value
    assert v["kind"] == "chart_suggestions" and v["suggestions"]
    assert v["suggestions"][0]["svg"].startswith("<svg")
    assert all(e["data"] in v["datasets"] for e in v["suggestions"] + v["more"])


def test_a_bare_number_is_quiet(reg: SkillRegistry) -> None:
    got = reg.spec("charts.suggest").fn({"result": {"value": 3.0, "unit": "kg"}}, None)
    assert got.ok and got.value["suggestions"] == [] and got.value["quiet"]


def test_render_refuses_a_bad_spec_with_reasons(reg: SkillRegistry) -> None:
    got = reg.spec("charts.render").fn({"spec": {"schema_version": "2.0", "kind": "pie"}}, None)
    assert not got.ok and got.errors


def test_an_oversized_result_goes_by_reference() -> None:
    xs = [f"2026-01-01T{h:02d}:{m:02d}:00Z" for h in range(24) for m in range(60)]
    big = {"series": [{"name": f"s{k}", "unit": "W", "x": xs, "y": [float((i * k) % 97) for i in range(len(xs))]}
                      for k in range(1, 7)]}
    out = block.build(big, "trend", cap=4096)
    assert out is not None and out["datasets"] == {} and out["data_ref"]
    held = block.fetch(out["data_ref"])
    assert held and all(e["data"] in held for e in out["suggestions"])
    assert out["suggestions"][0].get("svg"), "the first chart is still drawn inline"


def test_a_data_read_becomes_one_series_set() -> None:
    reads = [("data__series", {"site": "s1", "target": "inlet"}, _series_envelope("inlet", "degC", 40)),
             ("data__series", {"site": "s1", "target": "flow"}, _series_envelope("flow", "kg/s", 3))]
    b = answer.block_for_turn("compare inlet and flow", reads)
    assert b is not None
    assert [e["id"] for e in b["suggestions"]][:2] == ["line-dual", "scatter"]
    assert b["source"] == {"verbs": ["data.series"], "sites": ["s1"]}


def test_a_grouped_aggregate_becomes_categories() -> None:
    env = {"value": {"data": {"fn": "count", "group_by": ["site"], "groups": [
        {"site": "a", "value": 104, "rows": 1}, {"site": "b", "value": 27, "rows": 1},
        {"site": "c", "value": 8, "rows": 1}]}}}
    b = answer.block_for_turn("which site has the most rows", [("data__aggregate", {}, env)])
    assert b is not None and [e["id"] for e in b["suggestions"]] == ["bar-horizontal", "pie"]


def test_a_refused_read_or_a_non_data_tool_carries_no_chart() -> None:
    reads: list[Any] = []
    answer.collect(reads, "files__read", {}, {"value": {"data": {"series": []}}})
    answer.collect(reads, "data__series", {}, {"error": "refused"})
    assert answer.block_for_turn("anything", reads) is None


def test_the_agent_emits_one_block_at_the_end_of_a_web_turn() -> None:
    from axiom.extensions.builtins.chat.agent import ChatAgent
    from axiom.extensions.builtins.chat.scope import ChatScope

    sent: list[dict[str, Any]] = []

    class R:
        def render_chart_suggestions(self, b: dict[str, Any]) -> None:
            sent.append(b)

    agent = ChatAgent.__new__(ChatAgent)
    agent._render = R()  # type: ignore[attr-defined]
    scope = ChatScope()
    scope.view_context = {"schema": "axiom.view_context/1", "actions": []}
    agent._collect_chart_read("data__series", {"target": "inlet"}, _series_envelope("inlet", "degC", 40),
                              scope=scope)
    agent._collect_chart_read("data__series", {"target": "outlet"}, _series_envelope("outlet", "degC", 50),
                              scope=scope)
    agent._emit_chart_suggestions("how did they behave last week", scope=scope)
    assert len(sent) == 1
    assert [e["id"] for e in sent[0]["suggestions"]] == ["line", "box", "histogram"]
    assert scope.chart_reads == []


def test_a_chat_page_that_sends_no_view_context_still_gets_its_chart() -> None:
    """The plain chat page publishes no view, so ``view_context`` is None: the chart must still draw."""
    from axiom.extensions.builtins.chat.agent import ChatAgent
    from axiom.extensions.builtins.chat.scope import ChatScope

    sent: list[dict[str, Any]] = []

    class R:
        def render_chart_suggestions(self, b: dict[str, Any]) -> None:
            sent.append(b)

    agent = ChatAgent.__new__(ChatAgent)
    agent._render = R()  # type: ignore[attr-defined]
    scope = ChatScope()
    assert scope.view_context is None
    agent._collect_chart_read("data__series", {"target": "inlet"}, _series_envelope("inlet", "degC", 40),
                              scope=scope)
    agent._emit_chart_suggestions("how did it behave last week", scope=scope)
    assert len(sent) == 1 and sent[0]["suggestions"][0]["id"] == "line"


def test_a_terminal_turn_collects_nothing() -> None:
    from axiom.extensions.builtins.chat.agent import ChatAgent
    from axiom.extensions.builtins.chat.scope import ChatScope

    agent = ChatAgent.__new__(ChatAgent)
    scope = ChatScope()
    agent._collect_chart_read("data__series", {}, _series_envelope("a", "W", 1), scope=scope)
    assert scope.chart_reads == []


def test_the_http_face(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    fastapi = pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from axiom.extensions.builtins.charts.api import register_routes

    monkeypatch.setenv("AXI_STATE_DIR", str(tmp_path))
    app = fastapi.FastAPI()
    router = fastapi.APIRouter()
    register_routes(router)
    app.include_router(router, prefix="/api/v1")
    c = TestClient(app)
    result = {"rows": [{"g": "a", "n": 3}, {"g": "b", "n": 5}]}
    r = c.post("/api/v1/charts/suggest", json={"result": result, "question": "which is most"})
    assert r.status_code == 200, r.text
    body = r.json()
    first = body["suggestions"][0]
    spec = {**first["spec"], "data": body["datasets"][first["data"]]}
    t0 = time.perf_counter()
    drawn = c.post("/api/v1/charts/render", json={"spec": spec, "width": 480})
    assert drawn.status_code == 200 and drawn.json()["svg"].startswith("<svg")
    assert (time.perf_counter() - t0) < 2.0
    bad = c.post("/api/v1/charts/render", json={"spec": {"schema_version": "2.0", "kind": "pie"}})
    assert bad.status_code == 422 and bad.json()["problems"]
    assert c.get("/api/v1/charts/data/nope").status_code == 404


def test_the_cli(capsys: pytest.CaptureFixture[str], tmp_path: Any) -> None:
    from axiom.extensions.builtins.charts.cli import main

    f = tmp_path / "r.json"
    f.write_text(json.dumps({"value": 92.0, "unit": "percent", "label": "share"}))
    assert main(["suggest", str(f), "-q", "how close to the target"]) == 0
    out = capsys.readouterr().out
    assert "gauge" in out and "Next:" in out


def test_a_hierarchy_read_offers_a_flame_graph() -> None:
    tree = {"name": "day", "unit": "rows", "children": [
        {"name": "a", "children": [{"name": "x", "value": 30}, {"name": "y", "value": 12}]},
        {"name": "b", "children": [{"name": "x", "value": 8}]}]}
    b = answer.block_for_turn("where does a day's data go", [("data__aggregate", {}, {"value": {"data": {"tree": tree}}})])
    assert b is not None and b["suggestions"][0]["id"] == "flame"
    assert "svg" in b["suggestions"][0]
