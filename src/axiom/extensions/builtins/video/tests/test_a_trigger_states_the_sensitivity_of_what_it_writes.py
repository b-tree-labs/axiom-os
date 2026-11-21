# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""VID-031..033, ADR-267: a trigger states the sensitivity of what it writes.

A trigger declares its class in code (``internal`` unless it says otherwise); each
of its events carries it, whatever the event says, and its feed in the source's
journal records it. ``restricted`` is the class a site controls by its own rules,
controlled like ``ec-controlled`` and ``itar``.
"""

from __future__ import annotations

import pytest

from axiom.extensions.builtins.data_platform.daq import envelope
from axiom.extensions.builtins.data_platform.daq.journal import (
    OverflowPolicy,
    validate_overflow_policy,
)
from axiom.extensions.builtins.video import ledger, runner
from axiom.extensions.builtins.video import state as vs
from axiom.extensions.builtins.video import triggers as vt
from axiom.extensions.builtins.video.bus import FrameBus
from axiom.extensions.builtins.video.frame import Frame

from .harness import until
from .journaled import legacy_path, records
from .trigger_fakes import RestrictedTrigger, ScriptedTrigger

FAKES = "axiom.extensions.builtins.video.tests.trigger_fakes"


def _frame(seq: int, stream: str = "cam-1") -> Frame:
    return Frame(
        stream=stream,
        ts=f"2026-11-10T12:00:{seq % 60:02d}.{seq // 60:06d}+00:00",
        pts=seq / 10,
        seq=seq,
        session_id="s1",
        array=None,
    )


def _catalog() -> vt.TriggerCatalog:
    return vt.TriggerCatalog(
        entries={
            "scripted": vt.TriggerEntry("scripted", f"{FAKES}:ScriptedTrigger", "test"),
            "restricted-scripted": vt.TriggerEntry(
                "restricted-scripted", f"{FAKES}:RestrictedTrigger", "test"
            ),
            "handmade": vt.TriggerEntry("handmade", f"{FAKES}:HandmadeRestricted", "test"),
            "mislabelled": vt.TriggerEntry("mislabelled", f"{FAKES}:MislabelledTrigger", "test"),
        }
    )


def _run(state_dir, attachments, frames=8):
    bus = FrameBus("cam-1")
    triggers = runner.TriggerSet("cam-1", bus=bus, state_dir=state_dir, catalog=_catalog())
    result = triggers.apply(attachments)
    try:
        for seq in range(frames):
            bus.publish(_frame(seq))
        return triggers, result
    except BaseException:
        triggers.stop()
        raise


# ---- VID-031: declared in code, carried by every event, recorded on the feed ----------


@pytest.mark.acceptance("VID-031")
def test_an_event_carries_its_triggers_class_and_internal_by_default():
    assert ScriptedTrigger({}).event(_frame(1), "occurred", "e1").sensitivity == "internal"
    assert RestrictedTrigger({}).event(_frame(1), "occurred", "e1").sensitivity == "restricted"
    assert (
        vt.TriggerEvent.from_dict(
            RestrictedTrigger({}).event(_frame(1), "occurred", "e1").as_dict()
        ).sensitivity
        == "restricted"
    )


@pytest.mark.acceptance("VID-031")
def test_an_event_with_a_class_that_is_not_one_is_refused():
    with pytest.raises(ValueError, match="sensitivity"):
        vt.TriggerEvent(
            stream="cam-1",
            trigger="scripted@1.0.0",
            ts="2026-11-10T12:00:00+00:00",
            kind="occurred",
            incident_key="cam-1:scripted@1.0.0:e1",
            sensitivity="secret",
        )


@pytest.mark.acceptance("VID-031")
def test_each_feed_records_its_triggers_class(state_dir):
    triggers, result = _run(
        state_dir,
        [
            {"name": "scripted", "script": {"2": [["occurred", "a"]]}},
            {"name": "restricted-scripted", "script": {"3": [["occurred", "b"]]}},
            {"name": "handmade", "script": {"4": [["occurred", "c"]]}},
        ],
    )
    try:
        assert result["errors"] == []
        until(lambda: len(records(state_dir, "cam-1")) >= 3)
    finally:
        triggers.stop()
    by_feed = {r.envelope.feed: r.record.sensitivity for r in records(state_dir, "cam-1")}
    assert by_feed == {
        "video.scripted": "internal",
        "video.restricted-scripted": "restricted",
        "video.handmade": "restricted",
    }


@pytest.mark.acceptance("VID-031")
def test_an_episode_closed_on_shutdown_keeps_its_class(state_dir):
    started = RestrictedTrigger({}).event(_frame(1), "started", "e1")
    older = ScriptedTrigger({}).event(_frame(2), "started", "e2").as_dict()
    older.pop("sensitivity")  # written by an earlier release
    vs.write_open_episodes(
        "cam-1", {started.incident_key: started.as_dict(), older["incident_key"]: older}, state_dir
    )
    closed = runner.close_open_episodes("cam-1", state_dir, reason="stopped")
    assert {e.incident_key: e.sensitivity for e in closed} == {
        started.incident_key: "restricted",
        older["incident_key"]: "internal",
    }
    assert {r.envelope.feed: r.record.sensitivity for r in records(state_dir, "cam-1")} == {
        "video.restricted-scripted": "restricted",
        "video.scripted": "internal",
    }


@pytest.mark.acceptance("VID-031")
def test_a_trigger_with_a_class_that_is_not_one_is_refused_at_attach(state_dir):
    triggers, result = _run(state_dir, [{"name": "mislabelled"}], frames=0)
    try:
        (error,) = result["errors"]
        assert "secret" in error and "mislabelled" in error
        assert ". Fix: " in error and "restricted" in error.split(". Fix: ", 1)[1]
        assert "mislabelled" not in triggers.runners
    finally:
        triggers.stop()


# ---- VID-032: restricted is a controlled class ---------------------------------------


@pytest.mark.acceptance("VID-032")
def test_restricted_is_a_controlled_class_a_journal_never_drops():
    assert "restricted" in envelope.SENSITIVITY_CLASSES
    assert "restricted" in envelope.CONTROLLED_SENSITIVITIES
    with pytest.raises(ValueError):
        validate_overflow_policy(
            OverflowPolicy.DROP_OLDEST, sensitivity="restricted", delivery_class="standard"
        )
    assert (
        validate_overflow_policy(
            OverflowPolicy.BLOCK_PRODUCER, sensitivity="restricted", delivery_class="standard"
        )
        is OverflowPolicy.BLOCK_PRODUCER
    )


# ---- VID-033: what a person can see ---------------------------------------------------


@pytest.mark.acceptance("VID-033")
def test_the_trigger_list_shows_each_triggers_class():
    rows = {r["name"]: r for r in _catalog().rows()}
    assert rows["restricted-scripted"]["sensitivity"] == "restricted"
    assert rows["scripted"]["sensitivity"] == "internal"


@pytest.mark.acceptance("VID-033")
def test_events_show_each_events_class_and_internal_for_what_an_earlier_release_wrote(state_dir):
    from axiom.infra.state import locked_append_jsonl

    older = ScriptedTrigger({}).event(_frame(1), "occurred", "e0").as_dict()
    older.pop("sensitivity")
    locked_append_jsonl(legacy_path(state_dir), older)
    ledger.append(RestrictedTrigger({}).event(_frame(2), "occurred", "e1"), state_dir)
    rows = ledger.read(state_dir)
    assert [r["sensitivity"] for r in rows] == ["internal", "restricted"]
