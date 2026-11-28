# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""``data.export`` pages the table door and writes all of a view, or says it did not."""

from __future__ import annotations

import json
import logging
import types

import pytest

from axiom.extensions.builtins.data_platform import export_file as ef
from axiom.extensions.builtins.data_platform.skills import export as ex
from axiom.infra.skills import SkillResult

SPEC = {"columns": [{"id": "ts", "type": "time"}, {"id": "channel"}, {"id": "value", "type": "number"},
                    {"id": "unit"}, {"id": "source_class"}, {"id": "site"}]}


class Door:
    """A stand-in for ``data.table``: windows of at most 1,000 rows, paged by cursor."""

    def __init__(self, total, *, fail_at=None, rate_refusals=0):
        self.total, self.fail_at, self.rate_refusals = total, fail_at, rate_refusals
        self.calls = []

    def row(self, i):
        return {"ts": f"2026-03-04T14:{i // 60 % 60:02d}:{i % 60:02d}Z", "channel": "loop.flow",
                "value": float(i), "unit": "kg/s", "source_class": "measured", "site": "example-site"}

    def __call__(self, params, ctx):
        self.calls.append(params)
        if self.rate_refusals:
            self.rate_refusals -= 1
            return SkillResult(ok=False, errors=["rate limit: 5.0/s (burst 20) for this caller class"])
        start = int(params.get("cursor") or 0)
        if self.fail_at is not None and start >= self.fail_at:
            return SkillResult(ok=False, errors=["statement timeout"])
        end = min(start + min(params["limit"], 1000), self.total)
        return SkillResult(ok=True, value={
            "rows": [self.row(i) for i in range(start, end)],
            "next": str(end) if end < self.total else None,
            "spec": SPEC, "applied": {"order": ["ts asc", "channel asc"]},
            "provenance": {"source": "gold.signals", "watermark": "2026-03-04T15:00:00Z", "reads": 1},
        })


def ctx(tmp_path, surface="cli"):
    return types.SimpleNamespace(
        state_dir=tmp_path / "state", surface=surface, logger=logging.getLogger("t"),
        principal=types.SimpleNamespace(handle="@someone:example", assured=True),
    )


def run(tmp_path, door, **params):
    params.setdefault("out", str(tmp_path / "out.csv"))
    return ex.export(params, ctx(tmp_path), table=door, sleep=lambda s: None)


def test_every_window_is_read_and_the_file_holds_the_whole_view(tmp_path):
    door = Door(2500)
    done = run(tmp_path, door, channels=["loop.flow"], start="2026-03-04T14:00:00Z",
               end="2026-03-04T15:00:00Z")
    assert done.ok, done.errors
    assert done.value["rows"] == 2500 and done.value["complete"] is True
    assert [c.get("cursor") for c in door.calls] == [None, "1000", "2000"]
    # The request reaches the door as predicates it binds, never as text it has to split.
    assert door.calls[0]["where"] == [
        {"column": "channel", "op": "in", "value": ["loop.flow"]},
        {"column": "ts", "op": "ge", "value": "2026-03-04T14:00:00Z"},
        {"column": "ts", "op": "lt", "value": "2026-03-04T15:00:00Z"},
    ]
    said = json.loads((tmp_path / "out.provenance.json").read_text())
    assert said["dataset"] == "signals" and said["site"] == "example-site"
    assert said["exported_by"] == "@someone:example" and said["surface"] == "cli"
    assert said["served"]["watermark"] == "2026-03-04T15:00:00Z"
    # A storage tier is the platform's own business: no file that leaves it names one.
    left = (tmp_path / "out.provenance.json").read_text() + done.value["text"]
    assert not any(tier in left.lower() for tier in ("gold", "silver", "bronze"))
    assert said["order"] == ["ts asc", "channel asc"]
    assert said["units"] == {"loop.flow": "kg/s"}
    assert ef.verify(tmp_path / "out.csv")[0]


def test_reaching_the_row_limit_is_written_down_as_not_complete(tmp_path):
    done = run(tmp_path, Door(2500), rows=1200)
    assert done.ok and done.value["rows"] == 1200
    assert done.value["complete"] is False
    assert "stopped at the row limit of 1,200" in done.value["bounded"][0]
    assert "NOT COMPLETE" in done.value["text"]
    assert json.loads((tmp_path / "out.provenance.json").read_text())["complete"] is False


def test_a_view_that_ends_exactly_at_the_limit_is_complete(tmp_path):
    done = run(tmp_path, Door(1000), rows=1000)
    assert done.ok and done.value["complete"] is True


def test_a_window_that_fails_part_way_leaves_no_file(tmp_path):
    done = run(tmp_path, Door(5000, fail_at=2000))
    assert not done.ok
    assert "stopped after 2,000 rows and nothing was kept" in done.errors[0]
    assert not (tmp_path / "out.csv").exists()
    assert not (tmp_path / "out.provenance.json").exists()


def test_a_rate_limit_is_waited_out_not_reported_as_a_failure(tmp_path):
    door = Door(1500, rate_refusals=3)
    done = run(tmp_path, door)
    assert done.ok and done.value["rows"] == 1500


def test_a_refusal_on_the_first_window_is_the_callers_answer(tmp_path):
    def refuse(params, ctx):
        return SkillResult(ok=False, errors=["no relation gold.nothing"],
                           unmet=[{"id": "dataset", "why": "unknown", "fix": "data tables"}])

    done = run(tmp_path, refuse, dataset="nothing")
    assert not done.ok and done.errors == ["no relation gold.nothing"]
    assert done.unmet and done.unmet[0]["fix"] == "data tables"


def test_no_rows_writes_nothing_and_says_where_to_look(tmp_path):
    done = run(tmp_path, Door(0))
    assert not done.ok and "no rows matched" in done.errors[0]
    assert not (tmp_path / "out.csv").exists()


def test_an_existing_file_is_not_replaced_without_being_told(tmp_path):
    assert run(tmp_path, Door(10)).ok
    again = run(tmp_path, Door(10))
    assert not again.ok and "already there" in again.errors[0]
    assert run(tmp_path, Door(10), overwrite=True).ok


def test_only_the_named_columns_are_written_in_that_order(tmp_path):
    done = run(tmp_path, Door(5), columns=["ts", "value"])
    assert done.ok and done.value["columns"] == ["ts", "value"]
    assert (tmp_path / "out.csv").read_text().splitlines()[0] == "ts,value"
    wrong = run(tmp_path, Door(5), columns=["ts", "nope"], out=str(tmp_path / "w.csv"))
    assert not wrong.ok and "no column nope" in wrong.errors[0]


@pytest.mark.parametrize("fmt", ["jsonl", "parquet", "xlsx"])
def test_the_other_formats_are_the_same_rows(tmp_path, fmt):
    if fmt == "parquet":
        pytest.importorskip("pyarrow")
    csv = run(tmp_path, Door(1200))
    other = run(tmp_path, Door(1200), format=fmt, out=str(tmp_path / f"o.{fmt}"))
    assert other.ok, other.errors
    assert other.value["sha256"] == csv.value["sha256"]


def test_off_the_terminal_the_file_goes_to_the_exports_folder(tmp_path):
    done = ex.export({}, ctx(tmp_path, surface="mcp"), table=Door(3), sleep=lambda s: None)
    assert done.ok
    assert done.value["path"].startswith(str(tmp_path / "state" / "exports"))


def test_more_rows_than_one_export_holds_is_refused_before_anything_is_read(tmp_path):
    door = Door(10)
    done = run(tmp_path, door, rows=ex.ROWS_MAX + 1)
    assert not done.ok and not door.calls


def test_verify_says_whether_the_file_is_still_the_same_rows(tmp_path):
    run(tmp_path, Door(50))
    same = ex.export_verify({"path": str(tmp_path / "out.csv")})
    assert same.ok and same.value["same"] is True
    text = (tmp_path / "out.csv").read_text().replace(",7.0,", ",7.5,")
    (tmp_path / "out.csv").write_text(text)
    changed = ex.export_verify({"path": str(tmp_path / "out.csv")})
    assert not changed.ok and changed.value["same"] is False
