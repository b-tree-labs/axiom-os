# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""W9: Chart on gold. A new feed reaches the catalog within one conform cycle with no
code change; a saved chart has one stable id that the page, the CLI and MCP draw alike;
a chart saved from chat is listed in Chart's catalog by that id; the series a chart
draws carries its marked spans.

MVP-03-A1, MVP-11-A2, MVP-03-A2, MVP-02-A3, MVP-18-A1. SQLite fixtures only: nothing here
touches a real database.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from axiom.extensions.builtins.webapp.catalog import models, store
from axiom.extensions.builtins.webapp.views import models as view_models


@pytest.fixture
def gold(tmp_path, monkeypatch):
    """A SQLite session with a ``gold`` schema attached, standing in for the node's gold tier."""
    engine = create_engine(f"sqlite:///{tmp_path / 'webapp.db'}", poolclass=StaticPool)
    gold_file = tmp_path / "gold.db"

    @event.listens_for(engine, "connect")
    def _attach(dbapi_conn, _record):  # noqa: ANN001
        dbapi_conn.execute(f"ATTACH DATABASE '{gold_file}' AS gold")

    models.Base.metadata.create_all(engine)
    view_models.Base.metadata.create_all(engine)
    s = Session(engine)
    s.execute(
        text(
            "CREATE TABLE IF NOT EXISTS gold.signals (site TEXT, feed TEXT, channel TEXT, "
            "ts TEXT, value REAL, unit TEXT)"
        )
    )
    s.commit()

    @contextlib.contextmanager
    def _provider():
        yield s

    monkeypatch.setattr(store, "_provider", _provider)
    yield s
    s.close()


def _land(
    s,
    feed: str,
    channel: str,
    n: int,
    *,
    site: str = "site-a",
    unit: str = "kPa",
    start: datetime | None = None,
    value: float | None = 1.0,
) -> None:
    """What a conform pass leaves in gold for a feed: rows, nothing else."""
    t0 = start or datetime(2026, 10, 30, tzinfo=UTC)
    for i in range(n):
        s.execute(
            text("INSERT INTO gold.signals VALUES (:a,:b,:c,:d,:v,:u)"),
            {
                "a": site,
                "b": feed,
                "c": channel,
                "d": (t0 + timedelta(minutes=i)).isoformat(),
                "v": value,
                "u": unit,
            },
        )
    s.commit()


def _ctx(tmp_path, registry=None, principal: str = ""):
    from axiom.infra.principal import PrincipalContext
    from axiom.infra.skills import SkillContext, SkillRegistry

    extra = {"principal": PrincipalContext(handle=principal)} if principal else {}
    return SkillContext(
        registry=registry or SkillRegistry(),
        state_dir=tmp_path,
        logger=logging.getLogger("t"),
        **extra,
    )


# --- MVP-03-A1 / MVP-11-A2: a new feed reaches the catalog within one conform cycle -------


def _registry_with_projection():
    from axiom.extensions.builtins.webapp.skills import project
    from axiom.infra.skills import SkillRegistry

    registry = SkillRegistry()
    registry.register("webapp.project", project.run, mutating=False)
    return registry


def _conform(monkeypatch, tmp_path, registry, landed) -> Any:
    """One conform pass whose writes are ``landed()`` (the bronze→silver→gold effect)."""
    from axiom.extensions.builtins.data_platform.skills import conform_run

    def fake(**_kw):
        n = landed()
        return {
            "rows_in": n,
            "rows_out": n,
            "errored": 0,
            "unknown_schema": {},
            "unmapped_connectors": [],
            "registered_without_site": [],
            "distributions_loaded": ["kit"],
            "applied": True,
            "writes": {"inserted": n},
        }

    monkeypatch.setattr(conform_run, "run_conform", fake)
    return conform_run.run(
        {"dsn": "postgresql://unused/x", "bronze_root": str(tmp_path)}, _ctx(tmp_path, registry)
    )


def test_mvp_03_a1_a_new_feed_appears_in_the_catalog_with_no_code_change(
    gold, tmp_path, monkeypatch
):
    registry = _registry_with_projection()
    _conform(monkeypatch, tmp_path, registry, lambda: (_land(gold, "alpha", "T1", 3), 3)[1])
    # A new connector and its channel map: data only. The same code, no new branch for it.
    result = _conform(
        monkeypatch,
        tmp_path,
        registry,
        lambda: (_land(gold, "beta-loop", "PT-11", 4, unit="bar"), 4)[1],
    )
    assert result.value["catalog_projected"] is True
    chans = store.read_channels(gold, "site-a")
    assert {(c["feed"], c["channel"]) for c in chans} == {("alpha", "T1"), ("beta-loop", "PT-11")}
    beta = next(c for c in chans if c["feed"] == "beta-loop")
    assert beta["rows"] == 4 and beta["unit"] == "bar"


def test_mvp_11_a2_the_catalog_shows_a_conformed_feed_within_one_conform_cycle(
    gold, tmp_path, monkeypatch
):
    registry = _registry_with_projection()
    assert store.read_channels(gold, "site-b") == []
    result = _conform(
        monkeypatch,
        tmp_path,
        registry,
        lambda: (_land(gold, "gamma", "flow", 2, site="site-b"), 2)[1],
    )
    # Listed after this ONE pass: no second schedule, no wait for an ingest-cadence projection.
    assert [c["channel"] for c in store.read_channels(gold, "site-b")] == ["flow"]
    assert any("catalog projected" in a for a in result.actions_taken)


def test_mvp_11_a2_a_pass_that_wrote_nothing_does_not_project(gold, tmp_path, monkeypatch):
    calls: list[int] = []
    from axiom.infra.skills import SkillRegistry, SkillResult

    registry = SkillRegistry()
    registry.register(
        "webapp.project", lambda p, c: (calls.append(1), SkillResult())[1], mutating=False
    )
    _conform(monkeypatch, tmp_path, registry, lambda: 0)
    assert calls == []


def test_mvp_11_a2_a_failed_projection_is_reported_and_never_fails_the_pass(
    gold, tmp_path, monkeypatch
):
    from axiom.infra.skills import SkillRegistry, SkillResult

    registry = SkillRegistry()
    registry.register(
        "webapp.project", lambda p, c: SkillResult(ok=False, errors=["db down"]), mutating=False
    )
    result = _conform(monkeypatch, tmp_path, registry, lambda: (_land(gold, "a", "b", 1), 1)[1])
    assert result.ok and result.value["catalog_projected"] is False
    assert any("catalog projection failed" in a and "db down" in a for a in result.actions_taken)


# --- MVP-03-A2 / MVP-02-A3: one stable id, drawn alike on page, CLI and MCP ---------------

_X = [f"2026-10-30T00:0{i}:00Z" for i in range(5)]
SPEC = {
    "schema_version": "2.0",
    "kind": "line",
    "title": "Inlet",
    "data": {"series": [{"name": "inlet", "unit": "degC", "x": _X, "y": [20, 21.5, 21, 22, 23.5]}]},
}
WHO = "@pat:site-a"


def _save_from_chat(spec: dict[str, Any], group: str = "From chat") -> str:
    """What appkit ``chat/chartSuggestions.saveChart`` does: stamp the spec's key, write the
    groups document through ``/api/v1/views/groups/last``."""
    from axiom.extensions.builtins.scidisplay.unified_spec import catalog_id
    from axiom.extensions.builtins.webapp.views import read_view, write_view

    keyed = {**spec, "id": catalog_id(spec)}
    doc = read_view(WHO, "groups") or {"groups": []}
    target = next((g for g in doc["groups"] if g["name"] == group), None)
    if target is None:
        target = {"name": group, "graphs": [], "layout": "2", "syncDates": False, "syncRun": False}
        doc["groups"].append(target)
    if not any(g.get("spec", {}).get("id") == keyed["id"] for g in target["graphs"]):
        target["graphs"].append(
            {
                "id": f"g{len(target['graphs']) + 1}",
                "site": "",
                "feeds": [],
                "series": [keyed["id"]],
                "spec": keyed,
                "kind": "line",
                "name": keyed["title"],
                "drawer": False,
            }
        )
    write_view(WHO, "groups", doc)
    return keyed["id"]


def test_mvp_03_a2_saving_the_same_spec_twice_yields_the_same_id(gold) -> None:
    from axiom.extensions.builtins.charts import saved
    from axiom.extensions.builtins.scidisplay.unified_spec import catalog_id

    first = _save_from_chat(SPEC)
    second = _save_from_chat(json.loads(json.dumps(SPEC)))
    assert first == second == catalog_id(SPEC) and first.startswith("chat-")
    assert [c["id"] for c in saved.listing(WHO)] == [first]
    # The browser's own key for this spec (appkit specKey, FNV-1a over JSON.stringify).
    assert catalog_id({"a": 'x\ny"\\'}) == "chat-cgj0yt"


def test_mvp_02_a3_a_chart_saved_from_chat_is_listed_in_charts_catalog_by_its_id(
    gold, tmp_path
) -> None:
    from axiom.extensions.builtins.charts.skills import saved as saved_skill

    chart_id = _save_from_chat(SPEC)
    listed = saved_skill({}, _ctx(tmp_path, principal=WHO))
    assert listed.ok
    assert listed.value["charts"] == [
        {"id": chart_id, "group": "From chat", "graph": "g1", "title": "Inlet", "kind": "line"}
    ]
    # Somebody else's catalog does not list it.
    assert saved_skill({}, _ctx(tmp_path, principal="@other:site-a")).value["charts"] == []


def test_mvp_03_a2_one_id_renders_identically_on_page_cli_and_mcp(
    gold, tmp_path, monkeypatch, capsys
) -> None:
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from axiom.extensions.builtins.charts import api, cli
    from axiom.extensions.builtins.charts.skills import bind
    from axiom.extensions.builtins.mcp.skill_tools import skill_tool_contribution
    from axiom.extensions.builtins.webapp.api import views as views_api
    from axiom.infra.skills import SkillRegistry

    chart_id = _save_from_chat(SPEC)

    # Page: GET /api/v1/charts/saved/{id}, as the signed-in person.
    monkeypatch.setattr(views_api, "principal_of", lambda request: WHO)
    app = FastAPI()
    from fastapi import APIRouter

    router = APIRouter(prefix="/api/v1")
    api.register_routes(router)
    app.include_router(router)
    page = TestClient(app).get(f"/api/v1/charts/saved/{chart_id}")
    assert page.status_code == 200, page.text
    page_value = page.json()

    # MCP: axiom_charts__saved, the same principal.
    registry = SkillRegistry()
    bind(registry)
    contrib = skill_tool_contribution(registry, ctx_factory=lambda: _ctx(tmp_path, registry, WHO))
    mcp_value = asyncio.run(contrib.dispatch["axiom_charts__saved"]({"id": chart_id}))["value"]

    # CLI: axi charts saved <id> --json, the same principal.
    import functools

    from axiom.infra.principal import PrincipalContext
    from axiom.infra.skills import SkillContext

    monkeypatch.setattr(
        cli, "SkillContext", functools.partial(SkillContext, principal=PrincipalContext(handle=WHO))
    )
    assert cli.main(["saved", chart_id, "--json"]) == 0
    cli_value = json.loads(capsys.readouterr().out)

    assert page_value["id"] == mcp_value["id"] == cli_value["id"] == chart_id
    assert page_value["spec"] == mcp_value["spec"] == cli_value["spec"]
    assert page_value["svg"] == mcp_value["svg"] == cli_value["svg"]
    assert page_value["svg"].startswith("<svg")


def test_mvp_03_a2_an_unknown_id_is_refused_and_anonymous_is_refused(gold, tmp_path) -> None:
    from axiom.extensions.builtins.charts.skills import saved as saved_skill

    assert not saved_skill({"id": "chat-nope"}, _ctx(tmp_path, principal=WHO)).ok
    from axiom.infra.principal import PrincipalContext
    from axiom.infra.skills import SkillContext, SkillRegistry

    nobody = SkillContext(
        registry=SkillRegistry(),
        state_dir=tmp_path,
        logger=logging.getLogger("t"),
        principal=PrincipalContext(handle=""),
    )
    assert not saved_skill({}, nobody).ok


# --- MVP-18-A1: the series a chart draws carries its marked spans --------------------------


def test_mvp_18_a1_the_chart_series_carries_held_spans_beside_its_points(gold) -> None:
    from axiom.extensions.builtins.webapp.catalog.series import bucketed_series

    t0 = datetime(2026, 10, 30, tzinfo=UTC)
    _land(gold, "alpha", "T1", 2, start=t0)
    _land(gold, "alpha", "T1", 2, start=t0 + timedelta(minutes=2), value=None)  # withheld
    _land(gold, "alpha", "T1", 1, start=t0 + timedelta(minutes=4))
    out = bucketed_series(
        gold, site="site-a", feed="alpha", channels=["T1"], bucket_s=60, source="gold.signals"
    )
    assert [p["ts"][11:16] for p in out["series"]] == ["00:00", "00:01", "00:04"]
    assert out["marks"] == [
        {
            "channel": "T1",
            "mark": "held",
            "start": "2026-10-30T00:02:00Z",
            "end": "2026-10-30T00:04:00Z",
            "reason": "value withheld by a quality verdict",
        }
    ]


def test_mvp_18_a1_an_unmarked_series_keeps_its_envelope(gold) -> None:
    from axiom.extensions.builtins.webapp.catalog.series import bucketed_series

    _land(gold, "alpha", "T1", 3)
    out = bucketed_series(
        gold, site="site-a", feed="alpha", channels=["T1"], bucket_s=60, source="gold.signals"
    )
    assert list(out) == ["series"]
