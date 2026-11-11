# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Acceptance tests for unit J1: the journal, one append-only index of what happened on a node.

Ticket: docs/working/logbook-units/J1.md in the product repo that holds the plan, built against
`docs/working/design-note-j1-journal.md`. The owner's constraints of 2026-11-10 are pinned here
too: the stored row for a signed entry has no payload field and none of the entry's content
(A1), and a row is denied to a caller who may not read the record it points at (A4).
"""

from __future__ import annotations

import json
import logging
import uuid
from pathlib import Path

import pytest

pytest.importorskip("fastapi")

import fastapi  # noqa: E402
from sqlalchemy import text  # noqa: E402
from sqlalchemy.exc import DBAPIError  # noqa: E402

from axiom.extensions.builtins.attest import service as attest_service  # noqa: E402
from axiom.extensions.builtins.attest import store as attest_store  # noqa: E402
from axiom.extensions.builtins.attest.tests.test_api import OP1, as_  # noqa: E402
from axiom.extensions.builtins.attest.tests.test_signing import (  # noqa: E402
    SITE,
    _round_check,
    _sign,
)
from axiom.extensions.builtins.journal import api as journal_api  # noqa: E402
from axiom.extensions.builtins.journal import ingest, lanes, rights, service, store  # noqa: E402
from axiom.extensions.builtins.journal.skills import SPECS, bind_default  # noqa: E402
from axiom.infra import audit_trail  # noqa: E402
from axiom.infra.bus import EventBus  # noqa: E402
from axiom.infra.db import engine_for, normalize_extension_name, provision_extension  # noqa: E402
from axiom.infra.principal import PrincipalContext  # noqa: E402
from axiom.infra.site_scope import SiteScope  # noqa: E402
from axiom.infra.skill_dispatch import CLI_SURFACE, MCP_SURFACE, invoke_capability  # noqa: E402
from axiom.infra.skills import SkillContext  # noqa: E402
from axiom.webauth.users import User  # noqa: E402

ME = "@op1:site-a"
ELSEWHERE = "@op3:site-b"
OP3 = User(user_id="op3", email="op3@example.org", name="Op Three", roles=("user",),
           site="site-b", attributes={"idp": "entra"})  # fmt: skip


# -- fixtures ------------------------------------------------------------------------------


@pytest.fixture
def journal_db(attest_db, monkeypatch):
    """The journal schema at migration head, beside the attest schema, in the throwaway database.

    `attest_db` already chose a per-test schema suffix, so both schemas share it."""
    schema = normalize_extension_name("journal")
    result = provision_extension("journal")
    assert result.ok, result.error
    store.reset_provider()
    audit_trail._reset_for_tests()
    try:
        yield schema
    finally:
        engine, _ = engine_for("journal")
        with engine.begin() as conn:
            conn.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))


@pytest.fixture
def bus(tmp_path):
    """A bus with a durable log, so `replay()` has something to replay, made the process
    default so attest's own publishes land on it."""
    from axiom.infra import bus as bus_mod

    b = EventBus(log_path=tmp_path / "bus.jsonl")
    bus_mod.set_default_eventbus(b)
    yield b
    bus_mod.set_default_eventbus(None)
    b.shutdown()


@pytest.fixture
def live(journal_db, bus, tmp_path, monkeypatch):
    """Ingest running on `bus`, catching up from `tmp_path`, with every lane permitted."""
    from axiom.extensions.builtins.journal import writer

    writer.forget_partitions()
    lanes.set_declared([])
    monkeypatch.setattr(rights, "_lane_decider", lambda caller, lane: True)
    rights.forget_decisions()
    ingest.stop()
    ing = ingest.start(bus=bus, state_dir=tmp_path, ticker=False, catch_up=False)
    ing.forget_failures()
    try:
        yield ing
    finally:
        ing.stop()
        rights.forget_decisions()
        writer.forget_partitions()
        lanes.reset()


def caller(handle=ME, sites=("site-a",)):
    return rights.Caller(handle=handle, sites=frozenset(sites) if sites is not None else None)


def ctx(state_dir, *, handle=ME, surface="mcp"):
    return SkillContext(
        registry=bind_default(), state_dir=state_dir, logger=logging.getLogger("test.j1"),
        principal=PrincipalContext(handle=handle, posture="sso", assured=True),
        surface=surface,
    )  # fmt: skip


def rows(**kw):
    return service.query(kw.pop("caller", caller()), **kw)["rows"]


def signed(node_signer, **content):
    return _sign(_round_check(**content), node_signer).record


# -- acceptance rows -----------------------------------------------------------------------


@pytest.mark.acceptance("LB-J1-A1")
def test_j1_a1_a_signed_entry_is_one_reference_and_never_a_copy(live, node_signer, journal_db):
    rec = signed(node_signer, reading="4.2", note="valve left open")
    found = [r for r in rows(lanes=["attestations"]) if r["subject"].endswith(".signed")]
    assert len(found) == 1
    row = found[0]
    assert row["ref"] == {"logbook": "demo_log", "seq": rec["seq"],
                          "attestation_id": rec["attestation_id"], "digest": rec["digest"]}  # fmt: skip
    assert row["lane"] == "attestations" and row["actor"] == ME
    assert row["site"] == SITE and row["logbook"] == "demo_log"
    assert row["link"] == f"/api/v1/attest/demo_log/records/{rec['attestation_id']}"
    # The stored row, every column: no payload field, and none of the entry's content.
    with store.session_scope() as s:
        cols = [c for (c,) in s.execute(text(
            "SELECT column_name FROM information_schema.columns WHERE table_schema = :schema "
            "AND table_name = 'journal_events'"), {"schema": journal_db})]  # fmt: skip
        stored = s.execute(text("SELECT * FROM journal_events WHERE seq = :seq"),
                           {"seq": row["seq"]}).mappings().one()  # fmt: skip
    assert not {"payload", "content", "fields", "record", "statement", "body"} & set(cols)
    blob = json.dumps(dict(stored), default=str)
    for value in ("4.2", "valve left open", "walkdown"):
        assert value not in blob, value
    assert rec["content"]["title"] in blob  # the brief's title is the display line


@pytest.mark.acceptance("LB-J1-A2")
def test_j1_a2_catch_up_fills_a_missed_event_once_and_replay_adds_nothing(
    live, bus, node_signer, tmp_path
):
    live.stop()
    rec = signed(node_signer)
    assert rows(lanes=["attestations"]) == []
    live.start(bus=bus, state_dir=tmp_path, ticker=False, catch_up=False)
    report = live.catch_up()
    assert report["attest_records"]["appended"] == 1
    assert [r["ref"]["attestation_id"] for r in rows(lanes=["attestations"])] == [
        rec["attestation_id"]
    ]
    bus.publish("attest.demo_log.signed", {"schema_version": 1, **attest_service.brief(rec)},
                source="attest")  # fmt: skip
    bus.replay()
    again = live.catch_up()
    assert again["attest_records"]["appended"] == 0
    assert len(rows(lanes=["attestations"])) == 1


@pytest.mark.acceptance("LB-J1-A3")
@pytest.mark.parametrize("statement", [
    "UPDATE journal_events SET summary = 'edited'",
    "DELETE FROM journal_events",
    "UPDATE journal_keys SET source_id = 'x'",
    "DELETE FROM journal_keys",
])  # fmt: skip
def test_j1_a3_the_database_refuses_to_change_or_remove_a_row(live, bus, statement):
    bus.publish("made_up.thing", {"n": 1}, source="t")
    assert rows(lanes=["platform"])
    with store.session_scope() as s, pytest.raises(DBAPIError, match="append-only"):
        s.execute(text(statement))
        s.commit()


@pytest.mark.acceptance("LB-J1-A4")
def test_j1_a4_a_caller_who_may_not_read_a_log_gets_no_row_and_no_count(
    live, node_signer, client, tmp_path, monkeypatch
):
    signed(node_signer)
    # service: the same row is there for site-a and absent, uncounted, for site-b
    mine = service.query(caller())
    assert [r["lane"] for r in mine["rows"]] == ["attestations"]
    theirs = service.query(caller(ELSEWHERE, ("site-b",)))
    assert theirs["rows"] == [] and theirs["hidden"] == {}
    assert "attestations" not in {
        lane["lane"]
        for lane in service.lanes(caller(ELSEWHERE, ("site-b",)))["lanes"]
        if lane["count"]
    }
    # API: the session's site is the scope
    router = fastapi.APIRouter(prefix="/api/v1")
    journal_api.register_routes(router)
    client.app.include_router(router)
    as_(client, OP1)
    body = client.get("/api/v1/journal?lanes=attestations").json()
    assert len(body["rows"]) == 1 and body["rows"][0]["ref"]["logbook"] == "demo_log"
    as_(client, OP3)
    other = client.get("/api/v1/journal?lanes=attestations").json()
    assert other["rows"] == [] and other["hidden"] == {}
    assert all(lane["count"] == 0 for lane in client.get("/api/v1/journal/lanes").json()["lanes"])
    client.cookies.clear()
    assert client.get("/api/v1/journal").status_code == 401
    # MCP and CLI: the credential's site scope is the scope
    scopes = {
        ME: SiteScope(sites=frozenset({"site-a"})),
        ELSEWHERE: SiteScope(sites=frozenset({"site-b"})),
    }
    monkeypatch.setattr(rights, "_scope_for", lambda handle: scopes[handle])
    c1 = ctx(tmp_path)
    r = invoke_capability(
        c1.registry, "journal.query", {"lanes": "attestations"}, c1, surface=MCP_SURFACE
    )
    assert r.ok and len(r.value["rows"]) == 1
    c3 = ctx(tmp_path, handle=ELSEWHERE)
    r = invoke_capability(
        c3.registry, "journal.query", {"lanes": "attestations"}, c3, surface=MCP_SURFACE
    )
    assert r.ok and r.value["rows"] == [] and r.value["hidden"] == {}
    cli = ctx(tmp_path, handle=ELSEWHERE, surface="cli")
    r = invoke_capability(
        cli.registry, "journal.query", {"lanes": "attestations"}, cli, surface=CLI_SURFACE
    )
    assert r.ok and r.value["rows"] == [] and r.value["hidden"] == {}
    # a lane GUARD denies is absent, with no count, for every caller
    monkeypatch.setattr(rights, "_lane_decider", lambda who, lane: lane != "platform")
    rights.forget_decisions()
    live.bus.publish("made_up.thing", {"n": 1}, source="t")
    seen = service.query(caller(sites=None))
    assert "platform" not in {r["lane"] for r in seen["rows"]} and "platform" not in seen["hidden"]
    assert "platform" not in {lane["lane"] for lane in service.lanes(caller(sites=None))["lanes"]}


@pytest.mark.acceptance("LB-J1-A5")
def test_j1_a5_an_unknown_subject_lands_in_platform_and_is_counted(live, bus):
    bus.publish("made_up.thing.happened", {"n": 1}, source="t")
    bus.publish("tidy.vitals", {"cpu": 1}, source="tidy")
    platform = rows(lanes=["platform"], caller=caller(sites=None))
    assert {r["subject"] for r in platform} == {"made_up.thing.happened", "tidy.vitals"}
    report = service.lanes(caller(sites=None))
    by_lane = {lane["lane"]: lane for lane in report["lanes"]}
    assert by_lane["platform"]["count"] == 2
    assert report["unmapped"] == {"made_up.thing.happened": 1}
    assert set(by_lane) == set(lanes.names()) == set(lanes.BUILTIN_LANES)
    assert lanes.lane_for("attest.demo_log.signed") == "attestations"
    assert lanes.lane_for("cli.command_started") == "commands"
    with pytest.raises(ValueError):
        lanes.check_lane("gossip")


@pytest.mark.acceptance("LB-J1-A6")
def test_j1_a6_paging_newest_first_is_stable_while_rows_arrive(live, bus):
    for n in range(5):
        bus.publish("made_up.tick", {"n": n}, source="t")
    who = caller(sites=None)
    first = service.query(who, lanes=["platform"], limit=2)
    assert len(first["rows"]) == 2 and first["next_cursor"]
    for n in range(5, 7):
        bus.publish("made_up.tick", {"n": n}, source="t")
    second = service.query(who, lanes=["platform"], limit=2, cursor=first["next_cursor"])
    third = service.query(who, lanes=["platform"], limit=2, cursor=second["next_cursor"])
    seqs = [r["seq"] for page in (first, second, third) for r in page["rows"]]
    assert seqs == sorted(seqs, reverse=True) and len(seqs) == len(set(seqs)) == 5
    assert third["next_cursor"] is None
    fresh = service.query(who, lanes=["platform"], limit=10)
    assert len(fresh["rows"]) == 7 and fresh["rows"][0]["seq"] > seqs[0]


@pytest.mark.acceptance("LB-J1-A7")
def test_j1_a7_nothing_writes_from_any_surface():
    assert {s.name for s in SPECS} == {"journal.query", "journal.lanes", "journal.verify"}
    assert all(s.side_effects is False and s.risk == "read" for s in SPECS)
    assert all(set(s.surfaces) <= {"cli", "mcp", "agent_tool"} for s in SPECS)
    router = fastapi.APIRouter(prefix="/api/v1")
    journal_api.register_routes(router)
    paths = {r.path: set(r.methods) for r in router.routes}
    assert set(paths) == {"/api/v1/journal", "/api/v1/journal/lanes", "/api/v1/journal/stream"}
    assert all(methods <= {"GET", "HEAD"} for methods in paths.values()), paths
    # No `axi journal` noun (ADR-243): the verbs arrive as `axi log timeline` in F5.
    manifest = (Path(__file__).resolve().parents[2] / "src" / "axiom" / "extensions" / "builtins"
                / "journal" / "axiom-extension.toml").read_text()  # fmt: skip
    assert 'kind = "cmd"' not in manifest


@pytest.mark.acceptance("LB-J1-A8")
def test_j1_a8_verify_reports_a_mismatch_and_changes_nothing(live, node_signer):
    rec = signed(node_signer)
    rec2 = signed(node_signer, reading="fine")
    clean = service.verify()
    assert clean["ok"] and clean["checked"] >= 2 and clean["mismatches"] == []
    with attest_store.session_scope() as s:
        s.execute(text("ALTER TABLE attest_records DISABLE TRIGGER attest_records_append_only"))
        s.execute(text("UPDATE attest_records SET record = jsonb_set(record, '{digest}', "
                       "'\"0000000000000000000000000000000000000000000000000000000000000000\"') "
                       "WHERE attestation_id = :id"), {"id": rec2["attestation_id"]})  # fmt: skip
        s.execute(text("ALTER TABLE attest_records ENABLE TRIGGER attest_records_append_only"))
        s.commit()
    before = rows(lanes=["attestations"])
    report = service.verify()
    assert not report["ok"]
    assert [(m["attestation_id"], m["reason"]) for m in report["mismatches"]] == [
        (rec2["attestation_id"], "digest")
    ]
    assert rows(lanes=["attestations"]) == before
    assert attest_service.record(rec["attestation_id"])["digest"] == rec["digest"]
    assert attest_service.record(rec2["attestation_id"])["digest"].startswith("0000")
    skill = ctx(live.state_dir, surface="cli")
    r = invoke_capability(skill.registry, "journal.verify", {}, skill, surface=CLI_SURFACE)
    assert not r.ok and "digest" in r.errors[0]


def test_j1_a_lane_an_extension_declares_needs_no_schema_change(live, bus):
    """The owner's rule (2026-11-10): the set of lanes is open, declared by extensions with a
    retention class, so a future `pipeline` lane needs no migration."""
    lanes.set_declared(lanes.parse_manifest_lanes(
        {"lanes": [{"name": "pipeline", "retention": "year", "patterns": ["pipeline.>"],
                    "description": "Runs of simulation and training pipelines"}]},
        owner="pipelines",
    ))  # fmt: skip
    try:
        bus.publish("pipeline.run.finished", {"run_id": "r-7", "outcome": "ok"}, source="pipelines")
        who = caller(sites=None)
        assert live.failures() == 0, live.last_error
        everything = service.query(who, limit=50)
        assert [r["subject"] for r in everything["rows"]] == ["pipeline.run.finished"], everything
        row = rows(lanes=["pipeline"], caller=who)[0]
        assert row["lane"] == "pipeline" and row["retention_class"] == "year"
        assert row["subjects"] == ["r-7"] and row["status"] == "ok"
        report = {lane["lane"]: lane for lane in service.lanes(who)["lanes"]}
        assert report["pipeline"]["count"] == 1 and report["pipeline"]["durable"] is False
        assert report["pipeline"]["retention"]["class"] == "year"
        assert "pipeline.run.finished" not in service.lanes(who)["unmapped"]
    finally:
        lanes.set_declared([])
    with pytest.raises(ValueError, match="unknown lane"):
        service.query(who, lanes=["pipeline"])


def test_j1_the_row_id_and_sequence_are_the_journals_own(live, bus):
    bus.publish("made_up.thing", {"n": 1}, source="t")
    row = rows(lanes=["platform"], caller=caller(sites=None))[0]
    uuid.UUID(row["id"])
    assert isinstance(row["seq"], int) and row["retention_class"] == "month"
    assert row["classification"] == "internal"
