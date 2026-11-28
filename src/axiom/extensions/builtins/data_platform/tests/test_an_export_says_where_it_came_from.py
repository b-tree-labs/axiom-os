# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""A file that leaves the platform says where it came from, and can be checked later."""

from __future__ import annotations

import datetime as dt
import json

import pytest

from axiom.extensions.builtins.data_platform import export_file as ef

COLUMNS = ["ts", "channel", "value", "unit", "source_class", "quality"]


def rows():
    start = dt.datetime(2026, 3, 4, 14, 0, tzinfo=dt.UTC)
    out = []
    for i in range(6):
        out.append(
            {
                "ts": start + dt.timedelta(seconds=i),
                "channel": "loop.flow" if i % 2 else "loop.inlet_temperature",
                "value": None if i == 4 else 20.0 + i,  # one reading voided to NULL
                "unit": "kg/s" if i % 2 else "degC",
                "source_class": "predicted" if i == 5 else "measured",
                "quality": "good",
            }
        )
    return out


def export(tmp_path, fmt, **more):
    return ef.write(
        rows(), tmp_path / f"window{ef.FORMATS[fmt]}", columns=COLUMNS, fmt=fmt,
        site="example-site", dataset="signals", time_column="ts",
        request={"where": ["channel in loop.flow,loop.inlet_temperature"]},
        exported_by="@someone:example", surface="cli", **more,
    )  # fmt: skip


@pytest.mark.parametrize("fmt", sorted(ef.FORMATS))
def test_every_format_carries_the_same_provenance_and_the_same_hash(tmp_path, fmt):
    if fmt == "parquet":
        pytest.importorskip("pyarrow")
    done = export(tmp_path, fmt)
    said = json.loads(done.sidecar_path.read_text())
    assert done.rows == said["rows"] == 6
    assert said["site"] == "example-site" and said["dataset"] == "signals"
    assert said["columns"] == COLUMNS
    assert said["first_ts"] == "2026-03-04T14:00:00Z" and said["last_ts"] == "2026-03-04T14:00:05Z"
    assert said["units"] == {"loop.flow": "kg/s", "loop.inlet_temperature": "degC"}
    # Measured and predicted rows in one window are reported as both, never as one label.
    assert said["source_class"] == ["measured", "predicted"]
    assert said["complete"] is True and said["bounded"] == []
    assert said["exported_by"] == "@someone:example" and said["surface"] == "cli"
    assert said["request"] == {"where": ["channel in loop.flow,loop.inlet_temperature"]}
    # The hash is over what the cells say, so the format does not change it.
    assert done.sha256 == export(tmp_path / "again", "csv").sha256


@pytest.mark.parametrize("fmt", sorted(ef.FORMATS))
def test_a_file_read_back_holds_the_rows_its_provenance_describes(tmp_path, fmt):
    if fmt == "parquet":
        pytest.importorskip("pyarrow")
    done = export(tmp_path, fmt)
    ok, said = ef.verify(done.data_path)
    assert ok, said
    back = list(ef.read_rows(done.data_path))
    assert len(back) == 6
    assert back[4]["value"] is None  # a voided reading is not a zero on the way out
    assert float(back[1]["value"]) == 21.0


def test_one_changed_value_is_noticed(tmp_path):
    done = export(tmp_path, "csv")
    text = done.data_path.read_text().replace("21.0", "21.5")
    done.data_path.write_text(text)
    ok, said = ef.verify(done.data_path)
    assert not ok and "a value differs" in said


def test_a_dropped_row_is_noticed(tmp_path):
    done = export(tmp_path, "jsonl")
    lines = done.data_path.read_text().splitlines()
    done.data_path.write_text("\n".join(lines[:-1]) + "\n")
    ok, said = ef.verify(done.data_path)
    assert not ok and "holds 5 rows" in said


def test_a_renamed_file_is_still_the_same_data(tmp_path):
    done = export(tmp_path, "csv")
    moved = tmp_path / "figure-3.csv"
    done.data_path.rename(moved)
    done.sidecar_path.rename(ef.sidecar_path(moved))
    assert ef.verify(moved)[0]


def test_parquet_and_xlsx_carry_the_provenance_inside_the_file(tmp_path):
    pytest.importorskip("pyarrow")
    done = export(tmp_path, "parquet")
    done.sidecar_path.unlink()  # the sidecar is lost; the file still says where it came from
    assert ef.read_provenance(done.data_path)["sha256"] == done.sha256
    assert ef.verify(done.data_path)[0]

    from openpyxl import load_workbook

    book = load_workbook(export(tmp_path / "x", "xlsx").data_path, read_only=True)
    said = {k: v for k, v in book["provenance"].iter_rows(values_only=True)}
    assert said["site"] == "example-site" and said["sha256"] == done.sha256


def test_xlsx_carries_the_provenance_inside_the_file(tmp_path):
    """xlsx does not need pyarrow. The cross-check against parquet is the test above."""
    from openpyxl import load_workbook

    done = export(tmp_path, "xlsx")
    done.sidecar_path.unlink()
    book = load_workbook(done.data_path, read_only=True)
    said = {k: v for k, v in book["provenance"].iter_rows(values_only=True)}
    assert said["site"] == "example-site" and said["sha256"] == done.sha256


def test_a_csv_without_its_sidecar_refuses_to_be_called_provenanced(tmp_path):
    done = export(tmp_path, "csv")
    done.sidecar_path.unlink()
    with pytest.raises(ef.ExportError, match="has no provenance"):
        ef.read_provenance(done.data_path)


def test_no_rows_writes_nothing(tmp_path):
    with pytest.raises(ef.ExportError, match="no rows matched"):
        ef.write([], tmp_path / "empty.csv", columns=COLUMNS)
    assert not list(tmp_path.iterdir())


def test_an_incomplete_export_says_so_and_why(tmp_path):
    done = export(tmp_path, "csv", complete=False, bounded=["stopped at the row limit of 6"])
    said = json.loads(done.sidecar_path.read_text())
    assert said["complete"] is False
    assert said["bounded"] == ["stopped at the row limit of 6"]


def test_rows_are_taken_as_they_arrive(tmp_path):
    seen = []

    def stream():
        for row in rows():
            seen.append(row["ts"])
            yield row

    done = ef.write(stream(), tmp_path / "s.csv", columns=COLUMNS, time_column="ts")
    assert done.rows == len(seen) == 6


def test_a_format_written_whole_refuses_a_window_it_cannot_hold(tmp_path, monkeypatch):
    monkeypatch.setattr(ef, "BUFFERED_ROW_MAX", 3)
    with pytest.raises(ef.ExportError, match="holds at most 3 rows"):
        export(tmp_path, "parquet")


def test_an_unknown_format_names_the_real_ones(tmp_path):
    with pytest.raises(ef.ExportError, match="csv, jsonl, parquet, xlsx"):
        ef.write(rows(), tmp_path / "x.tsv", columns=COLUMNS, fmt="tsv")
