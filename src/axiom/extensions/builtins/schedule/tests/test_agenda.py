# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""MVP-24: the agenda read (``ScheduledItem``) over the scheduler, attest
obligations and vault expiry, in site time, on every surface, writing nothing."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import os
import time as _time
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from axiom.extensions.builtins.schedule import api, items, store
from axiom.extensions.builtins.schedule.db_models import Base, ScheduleFireLog
from axiom.extensions.builtins.schedule.skills import bind
from axiom.infra.skills import SkillContext, SkillRegistry

NOW = datetime(2026, 11, 2, 15, 0, tzinfo=UTC)  # a Monday, after the clock change
SITE_TZ = "America/Chicago"
WINDOW = {"from": "2026-11-02", "days": 7}


class _Writes:
    def __init__(self) -> None:
        self.count = 0
        self.armed = False


@pytest.fixture
def agenda(tmp_path, monkeypatch):
    """A fixed fixture: three scheduler definitions plus a fire-log row, two
    obligations (one weekly, one every 30 minutes), two credential expiries."""
    monkeypatch.setenv(items.SITE_TZ_ENV, SITE_TZ)
    writes = _Writes()
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool, future=True)
    Base.metadata.create_all(engine)

    @event.listens_for(engine, "before_cursor_execute")
    def _count(conn, cursor, statement, params, context, executemany):  # noqa: ANN001
        if writes.armed and statement.lstrip().split(" ", 1)[0].upper() in {
            "INSERT", "UPDATE", "DELETE", "CREATE", "DROP", "ALTER", "REPLACE",
        }:
            writes.count += 1

    maker = sessionmaker(engine, future=True)

    @contextlib.contextmanager
    def provider():
        s = maker()
        try:
            yield s
        finally:
            s.close()

    store.set_provider(provider)
    start = datetime(2026, 11, 2, 0, 0, tzinfo=UTC)
    nightly = api.register(envelope={"actor": {"principal": "@ops:site"}},
                           cadence=api.Cadence(kind="interval", interval=timedelta(days=1)),
                           action="backups.run", description="Nightly backup", extension="backups",
                           now=start)
    api.register(envelope=None, cadence=api.Cadence(kind="interval", interval=timedelta(minutes=1)),
                 action="health.ping", description="Heartbeat", now=start)
    api.register(envelope=None,
                 cadence=api.Cadence(kind="one_shot", not_before=datetime(2026, 11, 6, 20, tzinfo=UTC)),
                 action="reports.quarterly", description="Quarterly report", now=start)
    with store.session_scope() as s:
        s.add(ScheduleFireLog(id="f1", schedule_id=str(nightly), fire_time_bucket=1, params_hash="h",
                              intended_fire_at=datetime(2026, 11, 2, 8, 0), outcome="success", attempt=1))
        s.commit()

    obligations = [
        {"site_id": "site-a", "logbook": "ops", "obligation": "weekly_check", "type": "weekly_check",
         "interval": {"kind": "run", "number": 4}, "due_at": datetime(2026, 11, 5, 15, tzinfo=UTC),
         "state": "ok", "severity": "alarm", "notify": ["@ben:axiom"], "every_minutes": 7 * 24 * 60},
        {"site_id": "site-a", "logbook": "ops", "obligation": "half_hourly", "type": "half_hourly",
         "interval": {"kind": "run", "number": 4}, "due_at": datetime(2026, 11, 2, 15, 20, tzinfo=UTC),
         "state": "warn", "severity": "alarm", "notify": ["@ben:axiom"], "every_minutes": 30},
    ]
    fetches: list[datetime] = []

    def fetch(now):
        fetches.append(now)
        return [dict(o) for o in obligations]

    state = tmp_path / "state"
    index = state / "secrets" / "foreign-credentials.json"
    index.parent.mkdir(parents=True)
    index.write_text(json.dumps({"version": 2, "credentials": {
        "gitlab-pat": {"name": "gitlab-pat", "expires_at": "2026-11-05", "owner": "@ben:axiom",
                       "fingerprint": "abc123"},
        "webhook": {"name": "webhook", "expires_at": "2026-11-04T18:00:00+00:00"},
    }}))

    items.set_providers([
        items.SchedulerProvider(),
        items.AttestObligationProvider(fetch=fetch),
        items.VaultExpiryProvider(state_dir=state),
    ])
    items.set_clock(lambda: NOW)
    writes.armed = True
    try:
        yield {"writes": writes, "index": index, "nightly": str(nightly), "state": state}
    finally:
        items.reset_providers()
        items.reset_clock()
        store.reset_provider()
        engine.dispose()


def _ctx(state_dir=None) -> SkillContext:
    reg = SkillRegistry()
    bind(reg)
    return SkillContext(registry=reg, state_dir=state_dir, logger=logging.getLogger("t"))


def _ids(value: dict) -> list[str]:
    return [i["id"] for i in value["items"]]


@pytest.mark.acceptance("MVP-24-A1")
def test_the_agenda_lists_runs_obligations_and_expiries_each_with_source_and_owner_link(agenda):
    got = items.items_list(WINDOW)
    kinds = {i["kind"] for i in got["items"]}
    assert kinds == {"run", "obligation", "expiry"}
    assert got["count"] == len(got["items"])
    for it in got["items"]:
        assert it["source"] in {"schedule", "attest", "vault"}
        assert it["owner_link"]["cli"], it
    by_title = {i["title"]: i for i in got["items"]}
    assert by_title["weekly_check (ops)"]["owner_link"]["cli"].endswith(
        "log ops due --site site-a")
    assert by_title["gitlab-pat expires"]["owner"] == "@ben:axiom"
    runs = [i for i in got["items"] if i["kind"] == "run"]
    nightly = [i for i in runs if i["title"] == "Nightly backup"]
    # Seven planned fires in the week, plus the one already in the fire log.
    assert len([i for i in nightly if i["status"] == "planned"]) == 7
    assert [i["status"] for i in nightly if i["status"] != "planned"] == ["done"]
    assert nightly[0]["owner"] == "@ops:site"
    assert nightly[0]["owner_link"]["cli"].endswith(f"schedule status {agenda['nightly']}")
    # A per-minute schedule is one collapsed line, not ten thousand events.
    assert [i["title"] for i in runs if i["metadata"].get("collapsed")] == ["Heartbeat (every 1m)"]
    assert "Quarterly report" in by_title
    assert all(s["ok"] for s in got["sources"])


@pytest.mark.acceptance("MVP-24-A1")
def test_a_source_that_fails_is_reported_not_silently_empty(agenda):
    class Broken:
        name = "attest"

        def list(self, window, *, now):
            raise RuntimeError("no database")

    got = items.items_list(WINDOW, using=[items.SchedulerProvider(), Broken()])
    assert {"name": "attest", "ok": False, "error": "RuntimeError: no database"} in got["sources"]
    assert got["count"] > 0


@pytest.mark.acceptance("MVP-24-A2")
def test_every_time_is_in_site_time_and_labelled(agenda):
    got = items.items_list(WINDOW)
    assert got["tz"] == SITE_TZ
    assert got["tz_label"] == "CT (UTC-6)"
    assert got["window"]["from"] == "2026-11-02T00:00:00-06:00"
    for it in got["items"]:
        assert it["timezone"] == SITE_TZ
        if not it["date_only"]:
            assert datetime.fromisoformat(it["start"]).utcoffset() == timedelta(hours=-6), it
    webhook = next(i for i in got["items"] if i["title"] == "webhook expires")
    assert webhook["start"] == "2026-11-04T12:00:00-06:00" and webhook["date_only"] is False


@pytest.mark.acceptance("MVP-24-A2")
@pytest.mark.parametrize("viewer_tz", ["Pacific/Auckland", "America/Los_Angeles", "UTC"])
def test_a_date_only_expiry_does_not_shift_with_the_viewers_zone(agenda, monkeypatch, viewer_tz):
    monkeypatch.setenv("TZ", viewer_tz)
    if hasattr(_time, "tzset"):
        _time.tzset()
    try:
        pat = next(i for i in items.items_list(WINDOW)["items"] if i["title"] == "gitlab-pat expires")
        assert pat["date_only"] is True
        assert pat["start"] == "2026-11-05"
        assert "DTSTART;VALUE=DATE:20261105" in items.to_ics(items.items_list(WINDOW))
    finally:
        monkeypatch.delenv("TZ")
        if hasattr(_time, "tzset"):
            _time.tzset()


def test_the_zone_label_names_daylight_time_by_its_generic_name():
    assert items.tz_label("America/Chicago", datetime(2026, 7, 1, tzinfo=UTC)) == "CT (UTC-5)"
    assert items.tz_label("UTC") == "UTC"
    assert items.tz_label("Asia/Kolkata", NOW) == "IST (UTC+5:30)"


def test_the_site_zone_falls_back_to_utc(monkeypatch):
    monkeypatch.setenv(items.SITE_TZ_ENV, "Not/AZone")
    assert items.site_timezone() == "UTC"


@pytest.mark.acceptance("MVP-24-A3")
def test_a_minute_scale_obligation_is_absent(agenda):
    got = items.items_list(WINDOW)
    obligations = [i for i in got["items"] if i["kind"] == "obligation"]
    assert [i["metadata"]["obligation"] for i in obligations] == ["weekly_check"]
    assert all("half_hourly" not in i["id"] for i in got["items"])


def _client(monkeypatch, signed_in=True):
    from fastapi import APIRouter, FastAPI
    from fastapi.testclient import TestClient

    from axiom.extensions.builtins.schedule import agenda_api

    monkeypatch.setattr(agenda_api, "_claims",
                        lambda request: {"sub": "u9", "roles": ["viewer"]} if signed_in else {})
    router = APIRouter()
    agenda_api.register_routes(router)
    app = FastAPI()
    app.include_router(router, prefix="/api/v1")
    return TestClient(app)


def _mcp(tool: str, args: dict) -> dict:
    from axiom.extensions.builtins.mcp.skill_tools import skill_tool_contribution

    reg = SkillRegistry()
    bind(reg)
    contrib = skill_tool_contribution(
        reg, ctx_factory=lambda: SkillContext(registry=reg, state_dir=None, logger=logging.getLogger("t")),
    )
    return asyncio.run(contrib.dispatch[tool](args))


def _chat(args: dict) -> dict:
    """The chat loop's path: the agent_tool projection, then the dispatch chokepoint."""
    from axiom.extensions.builtins.chat.skill_tools import (
        skills_to_tool_definitions,
        tool_to_skill_name,
    )
    from axiom.infra.skill_dispatch import CHAT_SURFACE, invoke_capability

    ctx = _ctx()
    tools = {t.name: t for t in skills_to_tool_definitions(ctx.registry, namespace="schedule")}
    tool = next(n for n in tools if tool_to_skill_name(n) == "schedule.items_list")
    assert tools[tool].category.name == "READ"
    result = invoke_capability(ctx.registry, tool_to_skill_name(tool), args, ctx, surface=CHAT_SURFACE)
    assert result.ok, result.errors
    return result.value


@pytest.mark.acceptance("MVP-24-A4")
def test_page_cli_chat_and_mcp_return_the_same_items(agenda, monkeypatch, capsys):
    from axiom.extensions.builtins.mcp.skill_tools import mcp_tool_name
    from axiom.extensions.builtins.schedule import cli

    expected = _ids(items.items_list(WINDOW))
    assert expected

    web = _client(monkeypatch).get("/api/v1/schedule/agenda", params=WINDOW)
    assert web.status_code == 200, web.text
    assert _ids(web.json()) == expected
    assert web.json()["tz_label"] == "CT (UTC-6)"

    assert cli.cli(["agenda", "--from", "2026-11-02", "--days", "7", "--json"]) == 0
    assert _ids(json.loads(capsys.readouterr().out)) == expected

    assert _ids(_chat(dict(WINDOW))) == expected

    mcp = _mcp(mcp_tool_name("schedule.items_list"), dict(WINDOW))
    assert mcp["ok"], mcp
    assert _ids(mcp["value"]) == expected


@pytest.mark.acceptance("MVP-24-A4")
def test_one_item_shows_the_same_on_page_cli_and_mcp(agenda, monkeypatch, capsys):
    from axiom.extensions.builtins.mcp.skill_tools import mcp_tool_name
    from axiom.extensions.builtins.schedule import cli

    for it in items.items_list(WINDOW)["items"]:
        web = _client(monkeypatch).get(f"/api/v1/schedule/items/{it['id']}")
        assert web.status_code == 200, (it["id"], web.text)
        assert web.json()["item"] == it
    some = items.items_list(WINDOW)["items"][0]
    assert cli.cli(["item", "show", some["id"], "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["item"] == some
    mcp = _mcp(mcp_tool_name("schedule.items_show"), {"id": some["id"]})
    assert mcp["value"]["item"] == some
    assert _client(monkeypatch).get("/api/v1/schedule/items/vault:expiry:nope").status_code == 404


def test_the_page_is_for_signed_in_people_only(agenda, monkeypatch):
    assert _client(monkeypatch, signed_in=False).get("/api/v1/schedule/agenda").status_code == 401


def test_the_cli_table_and_ics_are_formats_of_the_same_read(agenda, capsys):
    from axiom.extensions.builtins.schedule import cli

    assert cli.cli(["agenda", "--from", "2026-11-02", "--days", "7"]) == 0
    table = capsys.readouterr().out
    assert "America/Chicago CT (UTC-6)" in table and "gitlab-pat expires" in table
    assert cli.cli(["agenda", "--from", "2026-11-02", "--days", "7", "--format", "ics"]) == 0
    ics = capsys.readouterr().out
    expected = _ids(items.items_list(WINDOW))
    assert ics.startswith("BEGIN:VCALENDAR") and ics.count("BEGIN:VEVENT") == len(expected)
    for item_id in expected:
        assert f"UID:{item_id}" in ics


def test_due_soon_is_the_needs_you_shape_for_one_person(agenda):
    got = items.due_soon("@ben:axiom", days=14)
    assert {i["id"] for i in got} == {
        "attest:obligation:site-a/ops/weekly_check/4",
        "vault:expiry:gitlab-pat",
    }
    shape = set(items.items_list(WINDOW)["items"][0])
    assert all(set(i) == shape for i in got)
    assert {i["kind"] for i in items.due_soon(None)} == {"obligation", "expiry"}
    assert items.due_soon("@someone-else:axiom") == []


def _snapshot(path) -> tuple[float, str]:
    return os.stat(path).st_mtime_ns, hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.mark.acceptance("MVP-24-A5")
def test_the_read_writes_nothing_to_any_source(agenda, monkeypatch, capsys):
    from axiom.extensions.builtins.mcp.skill_tools import mcp_tool_name
    from axiom.extensions.builtins.schedule import cli

    before = _snapshot(agenda["index"])
    state_files = sorted(p.relative_to(agenda["state"]) for p in agenda["state"].rglob("*"))

    items.items_list(WINDOW)
    items.due_soon("@ben:axiom")
    items.items_show(items.items_list(WINDOW)["items"][0]["id"])
    _client(monkeypatch).get("/api/v1/schedule/agenda", params=WINDOW)
    cli.cli(["agenda", "--from", "2026-11-02", "--days", "7", "--json"])
    _mcp(mcp_tool_name("schedule.items_list"), dict(WINDOW))
    _chat(dict(WINDOW))
    capsys.readouterr()

    assert agenda["writes"].count == 0  # not one INSERT/UPDATE/DELETE on the scheduler
    assert _snapshot(agenda["index"]) == before  # the vault index untouched
    assert sorted(p.relative_to(agenda["state"]) for p in agenda["state"].rglob("*")) == state_files

    # Negative control: the counter does see a real write.
    api.pause(agenda["nightly"], reason="control")
    assert agenda["writes"].count > 0


@pytest.mark.acceptance("MVP-24-A5")
def test_the_skills_are_registered_read_only_so_no_audit_row_is_written():
    from axiom.extensions.builtins.mcp.skill_tools import is_mcp_exposed

    reg = SkillRegistry()
    bind(reg)
    specs = reg.specs()
    for name in ("schedule.items_list", "schedule.items_show"):
        assert reg.is_mutating(name) is False
        assert specs[name].effective_risk == "read"
        assert is_mcp_exposed(specs[name])
        assert {"cli", "mcp", "agent_tool"} <= set(specs[name].surfaces)


def test_the_flag_is_declared_on_in_development_and_off_in_production():
    import tomllib
    from pathlib import Path

    from axiom.extensions.builtins.flags.registry import parse_manifest_flags

    manifest = tomllib.loads(
        (Path(items.__file__).parent / "axiom-extension.toml").read_text()
    )
    (flag,) = [f for f in parse_manifest_flags(manifest, owner="schedule") if f.key == "schedule_agenda"]
    assert flag.default == "off"
    assert flag.default_for("development") == "on" and flag.default_for("staging") == "on"
    assert flag.default_for("production") == "off"


def test_a_bad_window_is_refused_with_its_reason(agenda):
    r = _ctx().registry.invoke("schedule.items_list", {"days": "0"}, _ctx())
    assert not r.ok and "days" in r.errors[0]
    r = _ctx().registry.invoke("schedule.items_list", {"source": "nope"}, _ctx())
    assert not r.ok and "schedule" in r.errors[0]
    r = _ctx().registry.invoke("schedule.items_list", {"from": "2026-11-09", "to": "2026-11-02"}, _ctx())
    assert not r.ok
