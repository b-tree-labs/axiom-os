# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Rows dropped for want of a normalizer stay unread (#1344, finding 3).

In the M0 bundle rehearsal the first conform pass ran before the channel map
was readable, dropped 437 rows as ``unknown_schema``, and advanced the file's
watermark past them. Every later pass reported the file unchanged; only a
``--rebuild`` produced the rows. The watermark now stops just before a file's
first row that had no normalizer, so the pass after the normalizer is
installed reads it without a rebuild. Rows before it are not re-read.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from axiom.extensions.builtins.data_platform.conformance import NormalizerRegistry, conform_rows

KNOWN, UNKNOWN = "loop/frame-v1", "loop/aux-v1"


def _rec(i: int, ref: str) -> str:
    return json.dumps({"item_id": str(i), "schema_ref": ref, "row_hash": f"h{i}",
                       "row": {"ts": f"2026-10-01T00:00:0{i}+00:00", "ch": "T1", "v": i}})


def _norm(rec):
    yield {"feed": "loop.live", "channel": rec["row"]["ch"], "ts": rec["row"]["ts"],
           "value": float(rec["row"]["v"])}


class _Marks:
    """The FileWatermarks surface conform_rows uses, kept in memory."""

    def __init__(self):
        self.offsets: dict[str, int] = {}

    def enter_dir(self, d):
        pass

    def plan(self, path, st):
        off = self.offsets.get(str(path))
        if off is None:
            return SimpleNamespace(start=0, reason="new")
        if off >= st.st_size:
            return SimpleNamespace(start=off, reason="unchanged")
        return SimpleNamespace(start=off, reason="grown")

    def advance(self, path, *, offset, **_):
        self.offsets[str(path)] = offset

    def stats(self):
        return {}


def _bronze(tmp_path: Path, lines: list[str]) -> Path:
    f = tmp_path / "loop" / "_rows" / "2026-10-01" / "a.jsonl"
    f.parent.mkdir(parents=True)
    f.write_text("".join(x + "\n" for x in lines))
    return f


def _pass(root, registry, marks):
    out: list[dict] = []
    stats = conform_rows(root, registry, {"loop": "site-a"}, upsert=out.append,
                         watermarks=marks)
    return out, stats


def test_installing_the_normalizer_later_conforms_the_dropped_rows(tmp_path):
    lines = [_rec(1, KNOWN), _rec(2, UNKNOWN), _rec(3, KNOWN), _rec(4, UNKNOWN)]
    f = _bronze(tmp_path, lines)
    marks = _Marks()
    registry = NormalizerRegistry()
    registry.register(KNOWN, _norm)

    out, stats = _pass(tmp_path, registry, marks)
    assert len(out) == 2 and stats["unknown_schema"] == {UNKNOWN: 2}
    # Held just before the first unknown row, not at the end of the file.
    assert marks.offsets[str(f)] == len(lines[0]) + 1
    assert stats["files_held_for_unknown_schema"] == 1

    registry.register(UNKNOWN, _norm)
    out, stats = _pass(tmp_path, registry, marks)
    assert [r["value"] for r in out] == [2.0, 3.0, 4.0], "row 1 is not read again"
    assert stats["unknown_schema"] == {}
    assert marks.offsets[str(f)] == f.stat().st_size


def test_a_file_with_every_schema_known_advances_to_its_end(tmp_path):
    f = _bronze(tmp_path, [_rec(1, KNOWN), _rec(2, KNOWN)])
    marks = _Marks()
    registry = NormalizerRegistry()
    registry.register(KNOWN, _norm)
    _pass(tmp_path, registry, marks)
    assert marks.offsets[str(f)] == f.stat().st_size


def test_a_grown_file_whose_new_rows_are_unknown_keeps_its_watermark(tmp_path):
    f = _bronze(tmp_path, [_rec(1, KNOWN)])
    marks = _Marks()
    registry = NormalizerRegistry()
    registry.register(KNOWN, _norm)
    _pass(tmp_path, registry, marks)
    first = marks.offsets[str(f)]
    with f.open("a") as fh:
        fh.write(_rec(2, UNKNOWN) + "\n")
    _pass(tmp_path, registry, marks)
    assert marks.offsets[str(f)] == first
