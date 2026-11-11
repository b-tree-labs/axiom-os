# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""An item with no date and no span is UNSCHEDULED: valid, listed in its own
section, never drawn on the chart, never late."""

from __future__ import annotations

import copy

from axiom.extensions.builtins.program.model import ProgramData
from axiom.extensions.builtins.program.skills import publish, render, status
from axiom.extensions.builtins.program.skills._gantt import _rows
from axiom.extensions.builtins.program.tests.conftest import GENERIC_PROGRAM


def _raw():
    raw = copy.deepcopy(GENERIC_PROGRAM)
    raw["schedule"].append(
        {
            "id": "i-open",
            "label": "Undated work",
            "owner": "@dana:example-org",
            "lane": "alpha",
            "status": "proposed",
            "pct": 40,
        }
    )
    return raw


def test_gantt_omits_the_unscheduled_item():
    ids = [item["id"] for _lane, item in _rows(ProgramData(raw=_raw()))]
    assert "i-open" not in ids
    assert "i-one" in ids


def test_wiki_lists_unscheduled_in_its_own_section():
    page = publish.render_wiki_markdown(ProgramData(raw=_raw()), gantt_file=None)
    head, _, tail = page.partition("## Not yet scheduled")
    assert tail, "section missing"
    assert "i-open" in tail
    assert "alpha" in tail and "@dana:example-org" in tail
    assert "proposed" in tail and "40" in tail


def test_wiki_has_no_section_when_everything_is_dated():
    page = publish.render_wiki_markdown(ProgramData(raw=copy.deepcopy(GENERIC_PROGRAM)), gantt_file=None)
    assert "Not yet scheduled" not in page


def test_render_html_lists_unscheduled_in_its_own_section(write_data, tmp_path, ctx):
    raw = _raw()
    out = tmp_path / "site"
    result = render.run({"data": str(write_data(raw)), "out": str(out)}, ctx)
    assert result.ok, result.errors
    html = next(out.glob("*.html")).read_text()
    head, _, tail = html.partition("Not yet scheduled")
    assert tail
    assert "i-open" in tail
    assert "i-open" in head  # still shown in its lane too


def test_status_never_calls_an_unscheduled_item_late(write_data, ctx):
    path = write_data(_raw())
    result = status.run({"data": str(path), "scope": "schedule"}, ctx)
    assert result.ok
    item = next(i for i in result.value["items"] if i["id"] == "i-open")
    assert item["dates"] == {"start": None, "end": None, "date": None}
    assert item["scheduled"] is False
    assert all(i["scheduled"] for i in result.value["items"] if i["id"] != "i-open")
    drift = status.run({"data": str(path), "scope": "drift"}, ctx)
    assert not any("late" in str(f).lower() or "overdue" in str(f).lower() for f in drift.value.get("findings", []))


def test_priorities_rank_unscheduled_last_within_its_status():
    data = ProgramData(raw=_raw())
    ids = [e["id"] for e in status._priorities(data)]
    assert ids.index("i-open") > ids.index("i-three")
