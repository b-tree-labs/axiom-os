# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Reading a bronze tree without reading back what the gate refused.

Bronze holds three dispositions. Two of them exist precisely because the
provenance gate said no. An introspection verb that hands those back on
request undoes the refusal, so the boundary — counts yes, content no — is the
property these tests exist to hold.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from ..introspect import (
    MAX_PEEK_LIMIT,
    SUBDIRS,
    RefusedContent,
    UnknownConnector,
    freshness,
    inventory,
    peek,
)

CONNECTOR = "feed-a"


def _land(root: Path, *, subdir: str, day: str, files: int, rows: int = 1) -> None:
    d = root / CONNECTOR / subdir / day
    d.mkdir(parents=True, exist_ok=True)
    for n in range(files):
        lines = [
            json.dumps(
                {
                    "source_name": CONNECTOR,
                    "item_id": f"{day}-{n}-{r}",
                    "schema_ref": "loop/frame-v1",
                    "row_hash": f"h{n}{r}",
                    "row": {"t": float(r)},
                    "fetched_at": f"{day}T00:00:0{r}+00:00",
                }
            )
            for r in range(rows)
        ]
        (d / f"part{n}.jsonl").write_text("\n".join(lines) + "\n")


# --- inventory --------------------------------------------------------------


def test_inventory_counts_files_and_bytes_per_disposition(tmp_path: Path):
    _land(tmp_path, subdir="_rows", day="2026-09-27", files=3, rows=2)
    _land(tmp_path, subdir="_quarantine_rows", day="2026-09-27", files=2)

    out = inventory(tmp_path, connector=CONNECTOR)
    by_subdir = {d["subdir"]: d for d in out["data"]["connectors"][0]["dispositions"]}

    assert by_subdir["_rows"]["files"] == 3
    assert by_subdir["_quarantine_rows"]["files"] == 2
    assert by_subdir["_rows"]["bytes"] > 0
    assert by_subdir["_excluded"]["files"] == 0


def test_inventory_reports_the_day_range(tmp_path: Path):
    _land(tmp_path, subdir="_rows", day="2026-09-20", files=1)
    _land(tmp_path, subdir="_rows", day="2026-09-27", files=1)

    rows = inventory(tmp_path, connector=CONNECTOR)["data"]["connectors"][0]["dispositions"]
    rows_stat = next(d for d in rows if d["subdir"] == "_rows")

    assert (rows_stat["first_day"], rows_stat["last_day"]) == ("2026-09-20", "2026-09-27")
    assert rows_stat["days"] == 2


def test_inventory_marks_which_dispositions_are_readable(tmp_path: Path):
    _land(tmp_path, subdir="_rows", day="2026-09-27", files=1)

    dispositions = inventory(tmp_path, connector=CONNECTOR)["data"]["connectors"][0][
        "dispositions"
    ]
    readable = {d["subdir"]: d["readable"] for d in dispositions}

    assert readable == {"_rows": True, "_quarantine_rows": False, "_excluded": False}


def test_inventory_says_refused_content_is_counted_not_readable(tmp_path: Path):
    _land(tmp_path, subdir="_rows", day="2026-09-27", files=1)

    note = inventory(tmp_path, connector=CONNECTOR)["provenance"]["note"]

    assert "count only" in note


def test_inventory_covers_every_connector_when_none_is_named(tmp_path: Path):
    _land(tmp_path, subdir="_rows", day="2026-09-27", files=1)
    (tmp_path / "feed-b" / "_rows" / "2026-09-27").mkdir(parents=True)

    out = inventory(tmp_path)

    assert {c["connector"] for c in out["data"]["connectors"]} == {CONNECTOR, "feed-b"}


def test_an_unknown_connector_is_a_typed_error(tmp_path: Path):
    with pytest.raises(UnknownConnector):
        inventory(tmp_path, connector="nope")


def test_an_absent_root_is_not_an_error(tmp_path: Path):
    out = inventory(tmp_path / "never-created")
    assert out["data"]["connectors"] == []


# --- the refusal boundary ---------------------------------------------------


def test_peek_will_not_open_quarantined_items(tmp_path: Path):
    """They are in that directory because the gate said no."""
    _land(tmp_path, subdir="_quarantine_rows", day="2026-09-27", files=1)

    with pytest.raises(RefusedContent) as exc:
        peek(tmp_path, CONNECTOR, subdir="_quarantine_rows")

    assert "refused" in str(exc.value)
    assert "bronze_inventory" in str(exc.value)


def test_peek_will_not_open_excluded_items(tmp_path: Path):
    _land(tmp_path, subdir="_excluded", day="2026-09-27", files=1)

    with pytest.raises(RefusedContent):
        peek(tmp_path, CONNECTOR, subdir="_excluded")


def test_the_two_refused_dispositions_are_the_ones_the_gate_writes():
    assert SUBDIRS["_rows"] is True
    assert SUBDIRS["_quarantine_rows"] is False
    assert SUBDIRS["_excluded"] is False


# --- peek -------------------------------------------------------------------


def test_peek_returns_the_newest_day_first(tmp_path: Path):
    _land(tmp_path, subdir="_rows", day="2026-09-20", files=1)
    _land(tmp_path, subdir="_rows", day="2026-09-27", files=1)

    records = peek(tmp_path, CONNECTOR, limit=1)["data"]["records"]

    assert records[0]["item_id"].startswith("2026-09-27")


def test_peek_is_capped(tmp_path: Path):
    _land(tmp_path, subdir="_rows", day="2026-09-27", files=1, rows=50)

    out = peek(tmp_path, CONNECTOR, limit=10_000)

    assert len(out["data"]["records"]) == 50  # everything there is, but the cap holds
    assert f"{MAX_PEEK_LIMIT}" in out["provenance"]["method"]


def test_peek_defaults_to_a_small_sample(tmp_path: Path):
    _land(tmp_path, subdir="_rows", day="2026-09-27", files=1, rows=100)

    assert len(peek(tmp_path, CONNECTOR)["data"]["records"]) == 20


def test_peek_can_be_pinned_to_one_day(tmp_path: Path):
    _land(tmp_path, subdir="_rows", day="2026-09-20", files=1)
    _land(tmp_path, subdir="_rows", day="2026-09-27", files=1)

    records = peek(tmp_path, CONNECTOR, day="2026-09-20")["data"]["records"]

    assert all(r["item_id"].startswith("2026-09-20") for r in records)


def test_peek_reports_lines_it_could_not_parse_rather_than_dropping_them(tmp_path: Path):
    _land(tmp_path, subdir="_rows", day="2026-09-27", files=1)
    (tmp_path / CONNECTOR / "_rows" / "2026-09-27" / "bad.jsonl").write_text("{not json\n")

    out = peek(tmp_path, CONNECTOR)

    assert "could not be parsed" in out["provenance"]["note"]


# --- freshness --------------------------------------------------------------


def test_freshness_reports_the_newest_day_and_its_age(tmp_path: Path):
    day = (datetime.now(UTC) - timedelta(days=3)).date().isoformat()
    _land(tmp_path, subdir="_rows", day=day, files=1)

    row = freshness(tmp_path, connector=CONNECTOR)["data"]["connectors"][0]

    assert row["last_day"] == day
    assert row["age_days"] == 3


def test_freshness_says_a_connector_that_never_landed_anything(tmp_path: Path):
    (tmp_path / CONNECTOR).mkdir(parents=True)

    row = freshness(tmp_path, connector=CONNECTOR)["data"]["connectors"][0]

    assert row["last_day"] is None
    assert row["age_days"] is None


def test_freshness_puts_the_freshest_first(tmp_path: Path):
    recent = (datetime.now(UTC) - timedelta(days=1)).date().isoformat()
    old = (datetime.now(UTC) - timedelta(days=40)).date().isoformat()
    _land(tmp_path, subdir="_rows", day=old, files=1)
    (tmp_path / "feed-b" / "_rows" / recent).mkdir(parents=True)

    rows = freshness(tmp_path)["data"]["connectors"]

    assert [r["connector"] for r in rows] == ["feed-b", CONNECTOR]


def test_freshness_says_it_cannot_see_a_stalled_conform_pass(tmp_path: Path):
    """The distinction that decides whether you chase a DAQ or a pipeline."""
    _land(tmp_path, subdir="_rows", day="2026-09-27", files=1)

    note = freshness(tmp_path, connector=CONNECTOR)["provenance"]["note"]

    assert "conform" in note
