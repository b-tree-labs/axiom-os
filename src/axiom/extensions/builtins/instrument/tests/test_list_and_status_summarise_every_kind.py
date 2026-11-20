# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""INS-006, INS-007 and INS-008: the noun's own verbs, ``list`` and ``status``, are reads
over every kind, and each is a skill (ADR-056) that the command line wraps."""

from __future__ import annotations

import json

from axiom.extensions.builtins.instrument import skills
from axiom.extensions.builtins.instrument.commands import cli
from axiom.infra.skills import SkillRegistry


def _specs():
    registry = SkillRegistry()
    skills.bind(registry)
    return registry.specs()


def test_the_nouns_verbs_are_exactly_two_reads():
    specs = _specs()
    assert set(specs) == {"instrument.list", "instrument.status"}
    for name, spec in specs.items():
        assert spec.risk == "read", name
        assert spec.side_effects is False, name
        assert set(spec.surfaces) == {"cli", "mcp", "agent_tool"}, name
    assert cli.NOUN_VERBS == ("list", "status")


def test_every_noun_verb_is_its_skill():
    for verb in cli.NOUN_VERBS:
        assert cli.skill_for(verb) == f"instrument.{verb}"
        assert cli.skill_for(verb) in _specs()


def test_list_has_one_row_per_instrument_with_kind_name_health_state(call, kinds):
    result = call("instrument.list")
    assert result.ok, result.errors
    rows = result.value["instruments"]
    assert [(r["kind"], r["name"], r["health"], r["state"]) for r in rows] == [
        ("gauges", "temp", "ready", "reading"),
        ("gauges", "power", "ready", "reading"),
    ]
    assert result.value["failed"] == []


def test_list_kinds_names_each_kind_what_it_yields_and_where_it_came_from(call, kinds):
    result = call("instrument.list", kinds=True)
    assert result.ok, result.errors
    (row,) = result.value["kinds"]
    assert row["name"] == "gauges" and row["yields"] == "readings"
    assert row["state"] == "registered" and row["origin"]
    assert row["description"] == "Readings with a unit from a stand-in meter"


def test_status_is_one_summary_per_kind(call, kinds):
    result = call("instrument.status")
    assert result.ok, result.errors
    (summary,) = result.value["kinds"]
    assert summary["kind"] == "gauges" and summary["ok"] is True
    assert summary["counts"] == {"ready": 2}
    assert summary["summary"] == "2 channels, 2 ready"


def test_the_command_line_prints_list_kinds_and_status(kinds, capsys):
    assert cli.main(["list"]) == 0
    out = capsys.readouterr().out
    assert "gauges" in out and "temp" in out and "ready" in out
    assert cli.main(["list", "--kinds"]) == 0
    out = capsys.readouterr().out
    assert "gauges" in out and "readings" in out
    assert cli.main(["status"]) == 0
    assert "2 channels, 2 ready" in capsys.readouterr().out


def test_json_is_the_result_envelope(kinds, capsys):
    assert cli.main(["status", "--json"]) == 0
    envelope = json.loads(capsys.readouterr().out)
    assert envelope["ok"] is True
    assert envelope["value"]["kinds"][0]["kind"] == "gauges"


def test_with_no_kinds_list_says_what_to_do(monkeypatch, capsys):
    from axiom.extensions.builtins.instrument import registry

    empty = registry.KindCatalog()
    monkeypatch.setattr(registry, "catalog", lambda *, refresh=False: empty)
    assert cli.main(["list"]) == 0
    assert "no instrument kinds are registered" in capsys.readouterr().out


def test_the_reads_are_tools_an_assistant_can_call(state_dir):
    import asyncio

    from axiom.extensions.builtins.mcp.aggregation import AggregationRegistry

    surface = AggregationRegistry.from_node().build()
    names = {t.name for t in surface.tools if t.name.startswith("axiom_instrument__")}
    assert names == {"axiom_instrument__list", "axiom_instrument__status"}
    for tool in (t for t in surface.tools if t.name in names):
        assert tool.annotations.read_only_hint is True, tool.name
    answer = asyncio.run(surface.dispatch["axiom_instrument__status"]({}))
    assert answer["ok"], answer
