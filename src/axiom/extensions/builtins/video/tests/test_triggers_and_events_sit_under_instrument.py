# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Phase 2's verbs under the noun: ``<cli> instrument video events`` and ``list --triggers``
(ADR-256 D4), and an event that does not assume a frame (ADR-256 D7).

D7: triggers and events belong to every instrument kind. Video's ledger is the first
case, held now to the shared shape, so that a consumer's video trigger emits the same
events after the event type and ledger move under ``instrument``: nothing in an event
or a ledger row needs a frame.
"""

from __future__ import annotations

import argparse
import dataclasses
import json

import pytest

from axiom.extensions.builtins.instrument import registry
from axiom.extensions.builtins.instrument.commands import cli as instrument_cli
from axiom.extensions.builtins.video import ledger
from axiom.extensions.builtins.video.triggers import TriggerEvent, incident_key

from .scenes import crossing


@pytest.fixture
def real_kinds(monkeypatch):
    monkeypatch.setattr(registry, "_CACHE", None)
    yield registry.catalog(refresh=True)
    monkeypatch.setattr(registry, "_CACHE", None)


def _event(stream: str = "cam-1", kind: str = "started") -> TriggerEvent:
    return TriggerEvent(
        stream=stream,
        trigger="motion@0.1.0",
        ts="2026-11-11T12:00:00+00:00",
        kind=kind,
        incident_key=incident_key(stream, "motion@0.1.0", "e1"),
        score=0.12,
        detail={"method": "frame_diff", "roi": "full"},
    )


# ---- the verbs under the noun ----------------------------------------------------


def test_instrument_video_events_reads_the_ledger(real_kinds, state_dir, capsys):
    ledger.append(_event(), state_dir)
    assert instrument_cli.main(["video", "events", "--json"]) == 0
    events = json.loads(capsys.readouterr().out)["value"]["events"]
    assert [e["incident_key"] for e in events] == ["cam-1:motion@0.1.0:e1"]


def test_instrument_video_list_triggers_lists_motion(real_kinds, state_dir, capsys):
    assert instrument_cli.main(["video", "list", "--triggers", "--json"]) == 0
    rows = json.loads(capsys.readouterr().out)["value"]["triggers"]
    assert any(r["name"] == "motion" and r["state"] == "registered" for r in rows)


def test_no_events_yet_says_where_they_come_from(real_kinds, state_dir, capsys):
    assert instrument_cli.main(["video", "events"]) == 0
    assert "no events yet" in capsys.readouterr().out


def _tree():
    from axiom import axiom_cli
    from axiom.cli import command_space as cs
    from axiom.cli import completion_index as ci
    from axiom.extensions.builtins.instrument import skills as instrument_skills
    from axiom.extensions.builtins.video import skills as video_skills
    from axiom.infra.skills import SkillRegistry

    top = argparse.ArgumentParser(prog="axi")
    sub = top.add_subparsers(dest="subcommand")
    noun = instrument_cli.get_parser()
    node = sub.add_parser("instrument", help="Instruments", description=noun.description)
    axiom_cli._copy_subparsers(noun, node)
    reg = SkillRegistry()
    video_skills.bind(reg)
    instrument_skills.bind(reg)
    doc = cs.annotate(
        ci.build_doc(top, {}), reg.specs(), maps={"instrument": dict(instrument_cli.SKILL_FOR_VERB)}
    )
    return doc, cs.from_doc(doc)


def test_tab_reaches_events_and_list_triggers_with_their_skills(real_kinds):
    doc, _space = _tree()
    video = doc["commands"][0]["tree"]["subs"]["video"]["subs"]
    assert {n for a in video["events"]["args"] for n in a["names"]} >= {"name", "--since"}
    assert video["events"]["skill"] == "video.events" and video["events"]["risk"] == "read"
    assert "--triggers" in {n for a in video["list"]["args"] for n in a["names"]}


def test_next_lines_offer_events(real_kinds):
    from axiom.cli import next_steps

    _doc, space = _tree()
    lines = next_steps.suggest(("instrument", "video"), space=space, cache=None, recent=[], limit=6)
    assert any(line.startswith("axi instrument video events") for line in lines), lines


# ---- D7: an event assumes no frame -------------------------------------------------


def test_an_event_has_only_kind_neutral_fields():
    fields = {f.name for f in dataclasses.fields(TriggerEvent)}
    assert fields == {
        "stream",
        "trigger",
        "ts",
        "kind",
        "incident_key",
        "severity",
        "score",
        "detail",
    }
    for word in ("frame", "array", "pts", "seq", "session"):
        assert not any(word in f for f in fields), word


def test_a_ledger_row_is_plain_json_and_round_trips(state_dir):
    event = _event()
    ledger.append(event, state_dir)
    raw = ledger.ledger_path(state_dir).read_text(encoding="utf-8").strip()
    row = json.loads(raw)
    assert set(row) == set(event.as_dict())
    assert TriggerEvent.from_dict(row) == event


def test_motions_events_carry_no_frame_data():
    from axiom.extensions.builtins.video.motion import MotionTrigger

    trigger = MotionTrigger({})
    trigger.start()
    events = [e for frame in crossing() for e in trigger.observe(frame)]
    assert events, "the crossing opens an episode"
    for event in events:
        text = json.dumps(event.as_dict())  # raises on an array or any non-JSON value
        assert "ndarray" not in text
