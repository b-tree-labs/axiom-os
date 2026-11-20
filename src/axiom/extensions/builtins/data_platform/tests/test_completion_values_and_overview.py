# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""The data platform's Tab values, and what ``data`` with no verb says.

The provider asks the same read skills a person would, so these tests stand in
for those skills and never open a database or reach a node.
"""

from __future__ import annotations

import time
from types import SimpleNamespace

import pytest

from axiom.cli import completion_values as cv
from axiom.extensions.builtins.data_platform import completion_values as dcv
from axiom.infra.skills import SkillResult


class _Registry:
    def __init__(self, answers):
        self.answers = answers
        self.calls: list[tuple[str, dict]] = []

    def spec(self, name):
        verb = name.split(".", 1)[1]

        def fn(params, _ctx):
            self.calls.append((verb, dict(params)))
            got = self.answers[verb]
            if isinstance(got, Exception):
                raise got
            return SkillResult(ok=True, value=got)

        return SimpleNamespace(fn=fn)


ANSWERS = {
    "roles": {"data": {"roles": [
        {"role": "fluid_temperature", "sites": ["example-loop"]},
        {"role": "pump_speed", "sites": ["example-loop", "example-bench"]},
    ]}},
    "channels": {"data": {"channels": [
        {"feed": "example.epics", "channel": "TC1", "role": "fluid_temperature", "last_ts": "2026-10-01T23:59:00Z"},
        {"feed": "example.epics", "channel": "TC2", "role": "fluid_temperature", "last_ts": "2026-09-30T10:00:00Z"},
    ]}},
    "show": {"data": {"series": [
        {"t": "2026-09-30T00:00:00Z", "value": 120},
        {"t": "2026-09-29T00:00:00Z", "value": 0},
        {"t": "2026-10-01T00:00:00Z", "value": 88},
    ]}},
    "kit_verbs": {"verbs": [{"site": "example-loop", "name": "daily_summary"}]},
}


@pytest.fixture
def registry(monkeypatch):
    def make(answers=ANSWERS):
        reg = _Registry(dict(answers))
        monkeypatch.setattr(dcv, "_ctx", lambda: SimpleNamespace(registry=reg))
        return reg
    return make


def _pctx(binding=None, budget=30.0):
    return cv.ProviderContext(binding=binding or {}, deadline=time.monotonic() + budget)


def test_it_collects_every_kind_tab_offers(registry):
    reg = registry()
    out = dcv.collect(_pctx({"site": "example-loop"}))
    v = out["values"]
    assert v["site"][0] == "example-loop", "the bound site comes first"
    assert v["role"] == ["fluid_temperature", "pump_speed"]
    assert v["channel"] == ["TC1", "TC2"] and v["feed"] == ["example.epics"]
    assert v["day"] == ["2026-10-01", "2026-09-30"], "only days with readings, newest first"
    assert v["kit_verb"] == ["daily_summary"]
    assert out["per_site"]["example-bench"]["role"] == ["pump_speed"]
    assert not out["errors"]
    show = next(p for verb, p in reg.calls if verb == "show")
    assert show["end"] == "2026-10-02" and show["fn"] == "count", "the day scan ends at the last reading, not now"


def test_reads_are_bounded_to_a_few_sites(registry):
    many = {**ANSWERS, "roles": {"data": {"roles": [
        {"role": "r", "sites": [f"site-{i}" for i in range(10)]}]}}}
    reg = registry(many)
    dcv.collect(_pctx())
    assert sum(1 for verb, _ in reg.calls if verb == "channels") == dcv._MAX_SITES


def test_an_offline_node_names_the_kinds_it_could_not_read(registry):
    registry({**ANSWERS, "roles": ConnectionError("no route to node.example.org"),
              "kit_verbs": TimeoutError("timed out")})
    out = dcv.collect(_pctx({"site": "example-loop"}))
    assert {"role", "site", "kit_verb"} <= set(out["failed_kinds"])
    assert any("no route" in e for e in out["errors"])
    assert out["values"]["site"] == ["example-loop"], "the bound site is still known"


def test_it_stops_at_the_refresh_deadline(registry):
    reg = registry()
    dcv.collect(_pctx(budget=0.0))
    assert [verb for verb, _ in reg.calls] == ["roles"], "nothing after the deadline but the first read"


def test_bare_data_says_where_to_start_and_what_to_run(monkeypatch, capsys):
    from axiom.cli import next_steps
    from axiom.extensions.builtins.data_platform.commands import cli as data_cli

    monkeypatch.setattr(next_steps, "suggest",
                        lambda *a, **k: ["neut data latest --site example-loop --role pump_speed"])
    monkeypatch.setattr(data_cli.friendly_lang, "cli_name", lambda: "neut")
    assert data_cli.main([]) == 0
    out = capsys.readouterr().out
    for verb in data_cli.EVERYDAY_FIRST:
        assert f"  {verb}" in out
    tail = out.split("next:\n", 1)[1].splitlines()
    assert tail[0].strip() == "neut data roles", "a line that runs as written, always"
    assert tail[1].strip() == "neut data latest --site example-loop --role pump_speed"
    assert "<" not in out


def test_the_show_skill_says_its_target_is_a_role_or_a_channel():
    from axiom.extensions.builtins.data_platform import skills as data_skills
    from axiom.infra.skills import SkillRegistry

    reg = SkillRegistry()
    data_skills.bind(reg)
    assert reg.spec("data.show").values == {"target": "role|channel"}
    assert reg.spec("data.kit_verb").values == {"name": "kit_verb"}
