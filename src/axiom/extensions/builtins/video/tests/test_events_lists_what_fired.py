# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""VID-012: ``axi instrument video events [<source>] [--since <iso>]`` lists the ledger; ``list --triggers``
lists what can be attached, with version and origin, and what was refused and why.

``events`` is a read, so like ``list``, ``status`` and ``diagnose`` it is also an
MCP tool: "what fired on the door camera this morning" is a question an
assistant answers. A source's history outlives its declaration, so an
undeclared name is not an error.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from axiom.extensions.builtins.video import ledger
from axiom.extensions.builtins.video import triggers as vt
from axiom.extensions.builtins.video.commands import cli

from .harness import envelope, run_cli


def _append(state_dir, stream, kind, ts, episode="e1", severity="info", score=0.05):
    ledger.append(
        vt.TriggerEvent(
            stream=stream,
            trigger="motion@0.1.0",
            ts=ts,
            kind=kind,
            incident_key=f"{stream}:motion@0.1.0:{episode}",
            severity=severity,
            score=score,
            detail={"method": "frame_diff"},
        ),
        state_dir,
    )


@pytest.fixture
def history(state_dir):
    _append(state_dir, "cam-1", "started", "2026-11-10T12:00:00+00:00")
    _append(state_dir, "cam-1", "ended", "2026-11-10T12:00:09+00:00")
    _append(
        state_dir, "cam-2", "started", "2026-11-10T12:30:00+00:00", episode="e2", severity="warning"
    )
    return state_dir


def _skill(state_dir, **params):
    from axiom.extensions.builtins.video.skills import events

    return events.run(params, type("Ctx", (), {"state_dir": state_dir})())


def test_the_skill_lists_every_event_in_order(history):
    rows = _skill(history).value["events"]
    assert [(r["stream"], r["kind"]) for r in rows] == [
        ("cam-1", "started"),
        ("cam-1", "ended"),
        ("cam-2", "started"),
    ]


def test_the_skill_filters_by_source_and_since(history):
    assert [r["kind"] for r in _skill(history, name="cam-1").value["events"]] == [
        "started",
        "ended",
    ]
    rows = _skill(history, since="2026-11-10T12:00:05+00:00").value["events"]
    assert [r["ts"] for r in rows] == ["2026-11-10T12:00:09+00:00", "2026-11-10T12:30:00+00:00"]


def test_a_bad_since_is_refused_with_an_example(history):
    result = _skill(history, since="last tuesday")
    assert not result.ok and "since" in result.errors[0]


def test_an_undeclared_source_is_its_history_not_an_error(history):
    result = _skill(history, name="cam-9")
    assert result.ok and result.value["events"] == []


def test_events_is_a_verb_and_list_takes_triggers():
    assert "events" in cli.VERBS
    parser = cli.get_parser()
    args = parser.parse_args(["events", "cam-1", "--since", "2026-11-10T12:00:00+00:00"])
    assert cli._params(args) == {"name": "cam-1", "since": "2026-11-10T12:00:00+00:00"}
    assert cli._params(parser.parse_args(["events"])) == {}
    assert cli._params(parser.parse_args(["list", "--triggers"])) == {
        "kinds": False,
        "triggers": True,
    }


def test_kinds_and_triggers_are_one_or_the_other():
    with pytest.raises(SystemExit):
        cli.get_parser().parse_args(["list", "--kinds", "--triggers"])


def test_the_cli_lists_events_as_json(history):
    done = run_cli(["events", "cam-1", "--json"], state_dir=history)
    assert done.returncode == 0, done.stderr
    rows = envelope(done)["value"]["events"]
    assert [r["kind"] for r in rows] == ["started", "ended"]
    assert len({r["incident_key"] for r in rows}) == 1


def test_the_cli_prints_events_as_a_table(history):
    done = run_cli(["events"], state_dir=history)
    assert done.returncode == 0, done.stderr
    lines = done.stdout.splitlines()
    assert any("cam-1" in line and "started" in line and "motion@0.1.0" in line for line in lines)
    assert any("cam-2" in line and "warning" in line for line in lines)
    assert "cam-1:motion@0.1.0:e1" in done.stdout


def test_the_cli_says_when_nothing_has_fired(state_dir):
    done = run_cli(["events"], state_dir=state_dir)
    assert done.returncode == 0, done.stderr
    assert "no events" in done.stdout


def test_list_triggers_shows_motion_with_its_version_origin_and_that_it_is_a_heuristic(state_dir):
    done = run_cli(["list", "--triggers", "--json"], state_dir=state_dir)
    assert done.returncode == 0, done.stderr
    (motion,) = [t for t in envelope(done)["value"]["triggers"] if t["name"] == "motion"]
    assert (motion["version"], motion["origin"], motion["state"]) == (
        "0.1.0",
        "builtin",
        "registered",
    )
    assert "heuristic" in motion["description"]
    text = run_cli(["list", "--triggers"], state_dir=state_dir)
    assert "motion" in text.stdout and "0.1.0" in text.stdout and "builtin" in text.stdout


def test_events_answers_over_mcp(history):
    from axiom.extensions.builtins.mcp.aggregation import AggregationRegistry

    surface = AggregationRegistry.from_node().build()
    (tool,) = [t for t in surface.tools if t.name == "axiom_video__events"]
    assert tool.annotations.read_only_hint is True
    answer = asyncio.run(surface.dispatch["axiom_video__events"]({"name": "cam-2"}))
    assert answer["ok"], answer
    assert [r["incident_key"] for r in answer["value"]["events"]] == ["cam-2:motion@0.1.0:e2"]
    json.dumps(answer)


def test_status_counts_read_as_english(capsys):
    cli._print_triggers_of(
        {
            "attached": ["motion"],
            "triggers": [
                {
                    "name": "motion",
                    "trigger": "motion@0.1.0",
                    "state": "running",
                    "frames": 1,
                    "events": 1,
                    "errors": 1,
                    "dropped": 1,
                    "health": {"episodes": 1, "open": "k", "last_score": 0.1},
                }
            ],
        }
    )
    out = capsys.readouterr().out
    assert "1 frame," in out and "1 event," in out and "1 episode " in out and "1 error" in out
    assert "episodes" not in out and "errors" not in out
