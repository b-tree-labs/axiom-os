# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Follow-ups: records, skills, visibility, secrets (ADR-196).

The unit layer. It binds the program store and the schedule store to SQLite so
the state machine, the refusals and the arming run without a server; the real
Postgres and real-process proofs (kill -9, exactly once, escalation) live in
``test_followups_real_postgres.py``.
"""

from __future__ import annotations

import contextlib
import logging
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from axiom.extensions.builtins.program import followup_model as fm
from axiom.extensions.builtins.program import followup_store as store
from axiom.extensions.builtins.program import skills
from axiom.extensions.builtins.program.db_models import Base
from axiom.extensions.builtins.program.skills import followups
from axiom.extensions.builtins.schedule import store as pulse_store
from axiom.extensions.builtins.schedule.db_models import Base as PulseBase
from axiom.extensions.builtins.schedule.db_models import ScheduleDefinition
from axiom.infra.principal import PrincipalContext
from axiom.infra.skill_dispatch import CLI_SURFACE, MCP_SURFACE, invoke_capability
from axiom.infra.skills import SkillContext, SkillRegistry, SkillResult, SkillSpec

NOW = datetime(2026, 10, 20, 12, 0, tzinfo=UTC)
ME = "@bea:example-org"
OTHER = "@dov:example-org"


def _sqlite(base):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool, future=True)
    base.metadata.create_all(engine)
    maker = sessionmaker(engine, future=True)

    @contextlib.contextmanager
    def provider():
        s = maker()
        try:
            yield s
        finally:
            s.close()

    return engine, provider


@pytest.fixture
def world(monkeypatch, tmp_path):
    e1, p1 = _sqlite(Base)
    e2, p2 = _sqlite(PulseBase)
    store.set_provider(p1)
    pulse_store.set_provider(p2)
    clock = {"now": NOW}
    monkeypatch.setattr(followups, "_now", lambda: clock["now"])
    monkeypatch.setenv("AXIOM_MCP_CLIENT_PRINCIPAL", ME)
    registry = SkillRegistry()
    skills.bind(registry)
    registry.register_skill(SkillSpec(
        name="example.status", fn=lambda p, c: SkillResult(ok=True, value={"state": "ready"}),
        description="read-only status", inputs={"target": "str"}, side_effects=False, risk="read"))
    registry.register_skill(SkillSpec(
        name="example.drop", fn=lambda p, c: SkillResult(ok=True), description="drops", inputs={"target": "str"},
        side_effects=True, risk="destructive"))

    def ctx(surface=CLI_SURFACE):
        return SkillContext(registry=registry, state_dir=tmp_path, logger=logging.getLogger("t"), surface=surface,
                            principal=PrincipalContext(ME))

    def call(name, surface=CLI_SURFACE, **params):
        return invoke_capability(registry, f"program.{name}", params, ctx(surface), surface=surface)

    yield type("W", (), {"call": staticmethod(call), "clock": clock, "ctx": staticmethod(ctx)})
    store.reset_provider()
    pulse_store.reset_provider()
    e1.dispose()
    e2.dispose()


def _as(principal, monkeypatch):
    monkeypatch.setenv("AXIOM_MCP_CLIENT_PRINCIPAL", principal)


# ---- the pure rules --------------------------------------------------------


@pytest.mark.parametrize("text,delta", [("30m", 1800), ("24h", 86400), ("in 2 days", 172800), ("1w", 604800)])
def test_a_due_time_reads_a_duration(text, delta):
    assert fm.parse_due(text, now=NOW) == NOW + timedelta(seconds=delta)


def test_a_due_time_reads_an_iso_timestamp_and_treats_no_zone_as_utc():
    assert fm.parse_due("2026-10-21T09:00:00", now=NOW) == datetime(2026, 10, 21, 9, tzinfo=UTC)
    assert fm.parse_due("2026-10-21T09:00:00-05:00", now=NOW) == datetime(2026, 10, 21, 14, tzinfo=UTC)


@pytest.mark.parametrize("text", ["", "tomorrow-ish", "2020-01-01T00:00:00Z", "9999d"])
def test_a_due_time_that_is_unreadable_past_or_beyond_the_horizon_is_refused_with_a_fix(text):
    with pytest.raises(fm.FollowupError) as e:
        fm.parse_due(text, now=NOW)
    assert "Fix:" in str(e.value) or "past" in str(e.value) or "days out" in str(e.value)


def test_the_default_ladder_is_the_one_in_the_adr():
    assert [(r["after_s"], r["audience"]) for r in fm.DEFAULT_LADDER] == [
        (0, "owner"), (6 * 3600, "owner"), (24 * 3600, "requester"), (48 * 3600, "operator")]


# ---- add -------------------------------------------------------------------


def test_add_creates_the_record_journals_it_and_arms_a_one_shot_on_the_scheduler(world):
    r = world.call("followup_add", what="check on the drop-old task", due="24h", check="example.status",
                   check_args={"target": "orders_old"}, next_command="axi data drop-old orders_old")
    assert r.ok, r.errors
    v = r.value
    assert v["id"].startswith("fu-") and v["owner"] == ME and v["created_by"] == ME
    assert v["state"] == "open" and v["due_at"] == "2026-10-21T12:00:00Z" and v["armed"] is True
    row = store.get(v["id"])
    assert [e["kind"] for e in store.events(v["id"])] == ["created", "armed"]
    with pulse_store.session_scope() as s:
        d = s.get(ScheduleDefinition, row["schedule_id"])
        assert (d.action, d.state, d.reentrant, d.cadence_kind) == ("program.followup_fire", "active", True, "one_shot")
        assert d.capability_envelope == {"action": "program.followup_fire", "params": {"followup_id": v["id"]}}
        assert d.next_fire_at.replace(tzinfo=UTC) == NOW + timedelta(hours=24)


def test_a_record_survives_a_clock_that_cannot_be_armed(world, monkeypatch):
    from axiom.extensions.builtins.program import followup_clock

    def boom(*a, **k):
        raise RuntimeError("schedule schema is down")

    monkeypatch.setattr(followup_clock, "arm", boom)
    r = world.call("followup_add", what="rotate the key", due="2h")
    assert r.ok and r.value["armed"] is False
    assert "arm_failed" in [e["kind"] for e in store.events(r.value["id"])]


def test_a_check_must_be_a_registered_read_only_skill(world):
    assert "not a registered skill" in world.call("followup_add", what="x", due="1h", check="nope.none").errors[0]
    bad = world.call("followup_add", what="x", due="1h", check="example.drop")
    assert not bad.ok and "not read-only" in bad.errors[0] and "Fix:" in bad.errors[0]
    arg = world.call("followup_add", what="x", due="1h", check="example.status", check_args={"nope": 1})
    assert not arg.ok and "takes no argument" in arg.errors[0]
    assert not store.all_rows(), "a refused add leaves no record"


def test_no_secret_value_is_accepted_in_any_field(world):
    token = "glpat-" + "a1B2c3D4e5F6g7H8i9J0"
    for field, kw in [("what", dict(what=f"use {token}")), ("next_command", dict(what="x", next_command=f"curl -H {token}")),
                      ("check_args", dict(what="x", check="example.status", check_args={"target": token}))]:
        if "what" not in kw:
            kw["what"] = "x"
        r = world.call("followup_add", due="1h", **kw)
        assert not r.ok and "credential" in r.errors[0], field
        assert token not in " ".join(r.errors), "the refusal never echoes the value"
    r = world.call("followup_add", what="x", due="1h", check="example.status", check_args={"api_key": "v"})
    assert not r.ok
    assert not store.all_rows()


def test_an_owner_other_than_the_caller_needs_the_deputy_or_a_maintainer(world):
    r = world.call("followup_add", what="x", due="1h", owner=OTHER)
    assert not r.ok and r.value["refused"] == "forbidden" and "deputy" in r.errors[0]


# ---- who sees what ---------------------------------------------------------


def test_a_followup_made_over_mcp_by_one_principal_is_invisible_to_another(world, monkeypatch):
    mine = world.call("followup_add", surface=MCP_SURFACE, what="mine", due="1h").value["id"]
    assert world.call("followup_list", surface=MCP_SURFACE).value["count"] == 1
    _as(OTHER, monkeypatch)
    assert world.call("followup_list", surface=MCP_SURFACE).value["count"] == 0
    assert world.call("followup_owed", surface=MCP_SURFACE).value["summary"] == "nothing owed"
    for verb in ("followup_show", "followup_snooze", "followup_ack", "followup_done"):
        r = world.call(verb, surface=MCP_SURFACE, id=mine, by="2h")
        assert not r.ok and r.value["refused"] == "absent", verb
    assert store.get(mine)["state"] == "open"


def test_sharing_makes_it_visible_to_the_named_principal_only(world, monkeypatch):
    fid = world.call("followup_add", surface=MCP_SURFACE, what="shared", due="1h", shared_with=[OTHER]).value["id"]
    _as(OTHER, monkeypatch)
    assert world.call("followup_show", surface=MCP_SURFACE, id=fid).ok
    _as("@eli:example-org", monkeypatch)
    assert not world.call("followup_show", surface=MCP_SURFACE, id=fid).ok


# ---- the state machine -----------------------------------------------------


def test_snooze_moves_the_due_time_restarts_the_ladder_and_replaces_the_clock_row(world):
    fid = world.call("followup_add", what="x", due="2h").value["id"]
    old = store.get(fid)["schedule_id"]
    r = world.call("followup_snooze", id=fid, by="24h")
    assert r.ok and r.value["state"] == "snoozed" and r.value["due_at"] == "2026-10-21T12:00:00Z"
    row = store.get(fid)
    assert (row["cycle"], row["rung"]) == (1, 0) and row["schedule_id"] != old
    with pulse_store.session_scope() as s:
        assert s.get(ScheduleDefinition, old).state == "cancelled"
        assert s.get(ScheduleDefinition, row["schedule_id"]).state == "active"


def test_ack_needs_a_due_followup_and_stops_the_clock(world):
    fid = world.call("followup_add", what="x", due="2h").value["id"]
    early = world.call("followup_ack", id=fid)
    assert not early.ok and "not yet due" in early.errors[0] and "Fix:" in early.errors[0]
    store.update(fid, state="due")
    sid = store.get(fid)["schedule_id"]
    assert world.call("followup_ack", id=fid, note="on it").value["state"] == "acknowledged"
    assert store.get(fid)["schedule_id"] is None and store.get(fid)["next_action_at"] is None
    with pulse_store.session_scope() as s:
        assert s.get(ScheduleDefinition, sid).state == "cancelled"
    assert world.call("followup_ack", id=fid).ok, "ack is idempotent"


def test_done_closes_cancels_the_clock_and_keeps_the_journal(world):
    fid = world.call("followup_add", what="x", due="2h").value["id"]
    sid = store.get(fid)["schedule_id"]
    r = world.call("followup_done", id=fid, note="old table dropped", evidence=["https://example.org/change/1"])
    assert r.ok and r.value["state"] == "done" and r.value["closed_by"] == ME
    assert r.value["evidence"] == [{"label": "https://example.org/change/1", "url": "https://example.org/change/1"}]
    with pulse_store.session_scope() as s:
        assert s.get(ScheduleDefinition, sid).state == "cancelled"
    kinds = [e["kind"] for e in store.events(fid)]
    assert kinds[0] == "created" and kinds[-1] == "done"
    assert world.call("followup_snooze", id=fid, by="1h").ok is False, "a done follow-up cannot be snoozed"
    assert world.call("followup_list").value["count"] == 0
    assert world.call("followup_list", include_done=True).value["count"] == 1


def test_owed_groups_missed_and_due_first_and_is_quiet_when_nothing_is_owed(world):
    assert world.call("followup_owed").value["summary"] == "nothing owed"
    a = world.call("followup_add", what="upcoming", due="48h").value["id"]
    b = world.call("followup_add", what="overdue", due="1h").value["id"]
    world.clock["now"] = NOW + timedelta(hours=3)  # b is 2 h past due and nothing was delivered
    out = world.call("followup_owed").value
    assert [x["id"] for x in out["missed"]] == [b] and [x["id"] for x in out["open"]] == [a]
    assert out["summary"].startswith("1 missed")
    store.add_event(b, "delivered", ME, now=world.clock["now"], cycle=0, rung=0, dedup_key="d")
    assert world.call("followup_owed").value["missed"] == []


def test_show_returns_the_journal_and_the_exact_commands(world):
    fid = world.call("followup_add", what="x", due="2h").value["id"]
    v = world.call("followup_show", id=fid).value
    assert [j["kind"] for j in v["journal"]] == ["created", "armed"]
    assert f"axi program followup ack --id {fid}" in v["commands"]


# ---- the registered skills --------------------------------------------------


def test_the_skills_project_to_mcp_with_the_right_hints():
    pytest.importorskip("mcp")
    from axiom.extensions.builtins.mcp.skill_tools import mcp_tool_name, tool_annotations

    registry = SkillRegistry()
    skills.bind(registry)
    want = {"add": (False, False), "list": (True, False), "show": (True, False), "owed": (True, False),
            "snooze": (False, False), "ack": (False, False), "done": (False, False)}
    for verb, (read_only, destructive) in want.items():
        spec = registry.spec(f"program.followup_{verb}")
        assert "mcp" in spec.surfaces and mcp_tool_name(spec.name) == f"axiom_program__followup_{verb}"
        ann = tool_annotations(spec)
        assert (ann.read_only_hint, ann.destructive_hint) == (read_only, destructive), verb
    assert tool_annotations(registry.spec("program.followup_add")).idempotent_hint is False
    assert registry.spec("program.followup_ack").risk == "reversible-write"


def test_fire_and_sweep_are_not_mcp_tools():
    registry = SkillRegistry()
    skills.bind(registry)
    for name in ("program.followup_fire", "program.followup_sweep"):
        spec = registry.spec(name)
        if spec is not None:
            assert "mcp" not in spec.surfaces
