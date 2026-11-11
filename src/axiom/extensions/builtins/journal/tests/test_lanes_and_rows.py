# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""The lane map, statuses, retention floors and the row builders, with no database.

The acceptance tests in ``tests/logbook_acceptance/test_j1.py`` prove the store, ingest, rights
and surfaces against Postgres; these pin the pure parts so a change to the vocabulary is seen
without one.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from axiom.extensions.builtins.journal import lanes, rows


@pytest.fixture(autouse=True)
def no_extension_lanes():
    """Only the journal's own lanes, so these tests never walk the installed extensions."""
    lanes.set_declared([])
    yield
    lanes.reset()


def test_the_journals_own_lanes_are_declared_whole():
    assert {lane for _, lane in lanes.all_patterns()} <= set(lanes.names())
    assert set(lanes.names()) == set(lanes.BUILTIN_LANES)
    for lane in lanes.BUILTIN:
        assert lane.retention_class in lanes.RETENTION_CLASSES and lane.owner == "journal"


def test_an_extension_declares_a_lane_in_its_manifest():
    declared = lanes.parse_manifest_lanes(
        {"lanes": [{"name": "pipeline", "retention": "year", "patterns": ["pipeline.>"],
                    "description": "Runs of simulation and training pipelines"}]},
        owner="pipelines",
    )  # fmt: skip
    assert [lane.name for lane in declared] == ["pipeline"] and declared[0].owner == "pipelines"
    lanes.set_declared(declared)
    assert "pipeline" in lanes.names() and lanes.check_lane("pipeline") == "pipeline"
    assert lanes.lane_for("pipeline.run.finished") == "pipeline"
    assert lanes.retention("pipeline") == {
        "class": "year",
        "floor_days": 365,
        "effective_days": 365,
    }
    assert lanes.durable("pipeline") is False and lanes.patterns_for("pipeline") == ["pipeline.>"]
    # the journal's own patterns win: a declared lane cannot take attestations
    lanes.set_declared(
        lanes.parse_manifest_lanes(
            {"lanes": [{"name": "grabby", "retention": "month", "patterns": ["attest.>"]}]},
            owner="x",
        )
    )
    assert lanes.lane_for("attest.demo_log.signed") == "attestations"


def test_a_bad_lane_declaration_is_skipped_and_a_builtin_cannot_be_redeclared():
    bad = lanes.parse_manifest_lanes(
        {"lanes": [
            {"name": "Pipeline", "retention": "year"},
            {"name": "pipeline", "retention": "forever"},
            {"name": "pipeline", "retention": "year", "patterns": ["not a pattern"]},
            {"name": "platform", "retention": "year"},
            {"retention": "year"},
        ]},
        owner="x",
    )  # fmt: skip
    assert bad == []
    with pytest.raises(ValueError, match="journal's own lane"):
        lanes.declare("attestations", retention_class="year")
    lanes.declare("pipeline", retention_class="quarter", patterns=("pipeline.>",), owner="code")
    assert lanes.get("pipeline").owner == "code" and lanes.retention_class("pipeline") == "quarter"


@pytest.mark.parametrize(
    ("subject", "lane"),
    [
        ("attest.demo_log.signed", "attestations"),
        ("attest.demo_log.obligation_missed", "attestations"),
        ("tool.post_invoke", "tool_audit"),
        ("capability.telemetry", "tool_audit"),
        ("cli.command_started", "commands"),
        ("command.open_valve", "commands"),
        ("doctor.patch_failed", "agents"),
        ("tidy.escalation", "agents"),
        ("tidy.vitals", "platform"),
        ("rivet.pr_merged", "releases"),
        ("program.item_added", "program"),
        ("memory.appended", "memory"),
        ("schedule.fired", "platform"),
        ("bus.errors", "platform"),
        ("health.attestation_chain.failed", "platform"),
        ("made_up.thing", "platform"),
    ],
)
def test_subjects_map_to_their_lane(subject, lane):
    assert lanes.lane_for(subject) == lane


def test_an_unknown_subject_is_platform_and_unmapped():
    assert lanes.match("made_up.thing") == ("platform", None)
    assert lanes.match("tidy.vitals") == ("platform", "tidy.vitals")
    assert lanes.match("Not A Subject!") == ("platform", None)


def test_the_vocabulary_is_closed():
    with pytest.raises(ValueError, match="unknown lane"):
        lanes.check_lane("gossip")


@pytest.mark.parametrize(
    ("subject", "payload", "status"),
    [
        ("doctor.patch_failed", {}, "bad"),
        ("attest.demo_log.obligation_missed", {}, "bad"),
        ("attest.demo_log.obligation_warn", {}, "warn"),
        ("tidy.advisory", {}, "warn"),
        ("review.approved", {}, "ok"),
        ("rivet.pr_merged", {}, "ok"),
        ("tidy.vitals", {}, "info"),
        ("schedule.fired", {"outcome": "ok"}, "ok"),
    ],
)
def test_statuses(subject, payload, status):
    assert lanes.status_for(subject, payload) == status


def test_retention_floors_may_lengthen_never_shorten(monkeypatch):
    monkeypatch.setattr(lanes, "_requested_days", lambda lane: 7)
    assert lanes.retention("platform") == {"class": "month", "floor_days": 30, "effective_days": 30}
    monkeypatch.setattr(lanes, "_requested_days", lambda lane: 400)
    assert lanes.retention("platform")["effective_days"] == 400
    assert lanes.retention("attestations") == {
        "class": "lifetime",
        "floor_days": None,
        "effective_days": None,
    }


def test_a_brief_becomes_a_reference_and_nothing_else():
    brief = {
        "attestation_id": "a1", "seq": 3, "logbook": "demo_log", "site_id": "site-a",
        "entry_type": "ROUND_CHECK", "meaning": "performed", "title": "Round check",
        "signer": "@op1:site-a", "occurred_at": "2026-11-10T12:00:00+00:00", "digest": "d" * 64,
    }  # fmt: skip
    row = rows.from_brief(brief)
    assert row.lane == "attestations" and row.source_id == "a1" and row.actor == "@op1:site-a"
    assert row.ref == {"logbook": "demo_log", "seq": 3, "attestation_id": "a1", "digest": "d" * 64}
    assert row.link == "/api/v1/attest/demo_log/records/a1" and row.site == "site-a"
    assert row.summary == "ROUND_CHECK: Round check" and row.retention_class == "lifetime"
    assert row.at == datetime(2026, 11, 10, 12, tzinfo=UTC)


def test_the_tool_gateways_live_events_are_not_indexed():
    assert rows.from_event("tool.post_invoke", {"result": {"secret": "x"}}) is None
    assert rows.from_event("capability.telemetry", {"ms": 3}) is None


def test_a_command_row_drops_the_arguments():
    row = rows.from_event(
        "cli.command_ended",
        {
            "command_path": "vault",
            "args": ["set", "token", "hunter2"],
            "exit_code": 0,
            "principal": "ben",
        },
    )
    assert row.lane == "commands" and row.actor == "@ben" and row.status == "ok"
    assert "hunter2" not in row.summary and row.ref == {"command": "vault"}
    assert "hunter2" not in str(row)


def test_a_generic_event_keeps_ids_and_a_line_never_the_payload():
    row = rows.from_event(
        "schedule.fired",
        {
            "schedule_id": "s-1",
            "outcome": "ok",
            "payload": {"everything": "else"},
            "site_id": "site-a",
        },
    )
    assert row.lane == "platform" and row.subjects == ("s-1",) and row.site == "site-a"
    assert row.ref == {} and "everything" not in str(row)
    same = rows.from_event("schedule.fired", {"schedule_id": "s-1", "outcome": "ok",
                                              "payload": {"everything": "else"}, "site_id": "site-a"})  # fmt: skip
    assert same.source_id == row.source_id
