# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""INS-003 and INS-007: one bad package is that kind's problem only.

A kind that cannot be imported, whose entry is not a kind, or whose summaries
raise is listed with the reason and the fix, and the noun and every other kind
keep working.
"""

from __future__ import annotations

import pytest

from axiom.extensions.builtins.instrument import registry
from axiom.extensions.builtins.instrument.commands import cli

from .fakes import HERE, SulkingKind, extension


@pytest.fixture
def mixed(builtins, monkeypatch):
    ext = extension(
        builtins / "gauges-ext",
        [
            {"name": "gauges", "entry": f"{HERE}:GAUGES"},
            {"name": "missing", "entry": "axiom_instrument_no_such_module:KIND"},
            {"name": "explodes", "entry": f"{HERE}:explode"},
            {"name": "hollow", "entry": f"{HERE}:NOT_A_KIND"},
        ],
    )
    cat = registry.discover_kinds(extensions=[ext], entry_points=[], allow=())
    cat.register(SulkingKind())
    monkeypatch.setattr(registry, "catalog", lambda *, refresh=False: cat)
    return cat


def test_each_broken_kind_is_listed_with_why_and_the_fix(mixed):
    rows = {r["name"]: r for r in mixed.rows()}
    for name in ("missing", "explodes", "hollow"):
        assert rows[name]["state"] == "broken", name
        assert rows[name]["reason"], name
        assert rows[name]["fix"], name
    assert "this package is broken" in rows["explodes"]["reason"]
    assert rows["gauges"]["state"] == "registered"


def test_a_broken_kind_is_not_offered(mixed):
    assert mixed.names() == ["gauges", "sulky"]
    assert [k.name for k in mixed.kinds()] == ["gauges", "sulky"]


def test_running_a_broken_kind_exits_1_with_the_reason(mixed, capsys):
    assert cli.main(["explodes", "anything"]) == 1
    err = capsys.readouterr().err
    assert "this package is broken" in err and "fix:" in err


def test_the_other_kinds_still_run(mixed, capsys):
    assert cli.main(["gauges", "read", "temp"]) == 0
    assert "temp 21.5 degC" in capsys.readouterr().out


def test_list_and_status_answer_for_every_kind_that_can(call, mixed):
    listed = call("instrument.list")
    assert listed.ok, listed.errors
    assert {r["name"] for r in listed.value["instruments"]} == {"temp", "power"}
    assert [f["kind"] for f in listed.value["failed"]] == ["sulky"]
    assert "not answering" in listed.value["failed"][0]["error"]

    status = call("instrument.status")
    assert status.ok, status.errors
    by_kind = {s["kind"]: s for s in status.value["kinds"]}
    assert by_kind["gauges"]["ok"] is True
    assert by_kind["sulky"]["ok"] is False and "not answering" in by_kind["sulky"]["error"]
    for name in ("missing", "explodes", "hollow"):
        assert by_kind[name]["ok"] is False and by_kind[name]["fix"], name
