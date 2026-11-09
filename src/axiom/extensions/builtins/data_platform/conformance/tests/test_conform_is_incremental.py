# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""The conform pass is incremental: it reads only the bytes it has not read.

A pass that re-reads every bronze file every night does not scale with the
archive: on one node it re-read 30 million rows for 95 million no-op upserts,
took eight hours and 127 GB of memory, and a file landing after it started
waited a day. This is the fix, in the platform rather than in one site's
script: a durable watermark per bronze file in the database (path, size,
offset, a hash of the file's head and of the last line consumed), only new
bytes read, memory bounded by streaming, a checkpoint that commits the rows
and the watermarks together so a pass cut short resumes exactly, a rewritten
file noticed and re-read, and a rebuild that is explicit.

Against a real TimescaleDB (a ``tsdb-*`` container). The bronze files are
real files under a temporary root, written the way the ingest sink writes
them: one JSON record per line.
"""

from __future__ import annotations

import hashlib
import json
import tracemalloc
from pathlib import Path
from types import SimpleNamespace

import psycopg
import pytest

from axiom.extensions.builtins.data_platform.conformance import SILVER_DDL, NormalizerRegistry
from axiom.extensions.builtins.data_platform.conformance.runner import run_conform
from axiom.extensions.builtins.data_platform.conformance.watermarks import WATERMARKS_TABLE

SCHEMA_REF = "loop/frame-v1"
CONNECTOR = "loop"
SITE = "site-a"


def _normalize(rec):
    row = rec["row"]
    yield {
        "feed": "loop.live", "channel": row["ch"], "ts": row["ts"], "value": float(row["v"]),
        "unit": "degC", "quality": "ok", "source_class": "measured",
    }


def _register_connector(state_dir: Path) -> None:
    from axiom.extensions.builtins.data_platform.agents.plinth.connectors import (
        ConnectorConfig,
        save_connector,
    )

    save_connector(
        ConnectorConfig(name=CONNECTOR, kind="push", site=SITE, bronze_root=str(state_dir / "bronze"),
                        params={"schema_ref": SCHEMA_REF}),
        state_dir=state_dir,
    )


def _line(i: int, *, day: str = "2026-10-01", ch: str = "T1", v: float | None = None) -> str:
    # One instant per i. Silver's identity is the reading (ADR-206), so two
    # lines of one channel at one instant are ONE reading, not two rows.
    from datetime import datetime, timedelta

    ts = (datetime.fromisoformat(f"{day}T00:00:00+00:00") + timedelta(milliseconds=i)).isoformat()
    rec = {"item_id": f"{day}-{ch}-{i}", "schema_ref": SCHEMA_REF,
           "row_hash": hashlib.sha256(f"{day}|{ch}|{i}".encode()).hexdigest(),
           "row": {"ts": ts, "ch": ch, "v": float(i) if v is None else v}}
    return json.dumps(rec)


def _write(root: Path, name: str, lines: list[str], *, day: str = "2026-10-01", partial: str | None = None) -> Path:
    path = root / CONNECTOR / "_rows" / day / f"{name}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    text = "".join(line + "\n" for line in lines) + (partial or "")
    path.write_text(text)
    return path


def _append(path: Path, lines: list[str]) -> None:
    with path.open("a") as f:
        for line in lines:
            f.write(line + "\n")


@pytest.fixture
def env(timescale_dsn, tmp_path):
    with psycopg.connect(timescale_dsn, autocommit=True) as c:
        c.execute("DROP SCHEMA IF EXISTS silver CASCADE")
        c.execute("DROP SCHEMA IF EXISTS gold CASCADE")
        c.execute(f"DROP TABLE IF EXISTS {WATERMARKS_TABLE}")
        # Silver exists before any pass, so a dry run (which rolls its DDL
        # back with everything else) still leaves a table to count.
        for stmt in SILVER_DDL:
            c.execute(stmt)
    state = tmp_path / "state"
    state.mkdir()
    _register_connector(state)
    registry = NormalizerRegistry()
    registry.register(SCHEMA_REF, _normalize)
    root = tmp_path / "bronze"
    root.mkdir()

    def run(**over):
        kw = dict(bronze_root=root, dsn=timescale_dsn, state_dir=state, registry=registry, batch_size=100)
        kw.update(over)
        return run_conform(**kw)

    def count():
        with psycopg.connect(timescale_dsn) as c:
            return c.execute("SELECT count(*) FROM silver.signals").fetchone()[0]

    def marks():
        with psycopg.connect(timescale_dsn) as c:
            if c.execute("SELECT to_regclass(%s)", (WATERMARKS_TABLE,)).fetchone()[0] is None:
                return {}  # a full pass never creates the table
            return {r[0]: (r[1], r[2]) for r in c.execute(
                f"SELECT path, size, offset_bytes FROM {WATERMARKS_TABLE} WHERE root = %s", (str(root),))}

    return SimpleNamespace(root=root, run=run, count=count, marks=marks, dsn=timescale_dsn)


def test_a_second_pass_reads_no_byte_it_has_read(env):
    a = _write(env.root, "a", [_line(i) for i in range(500)])
    b = _write(env.root, "b", [_line(i, ch="T2") for i in range(500)])
    first = env.run()
    assert first["rows_in"] == 1000 and first["files_read"] == 2 and first["incremental"] is True
    assert env.count() == 1000
    second = env.run()
    assert second["rows_in"] == 0 and second["bytes_read"] == 0
    assert second["files_skipped"] == 2 and second["files_read"] == 0
    assert env.count() == 1000
    assert env.marks() == {
        str(a.relative_to(env.root)): (a.stat().st_size, a.stat().st_size),
        str(b.relative_to(env.root)): (b.stat().st_size, b.stat().st_size),
    }, "the watermark is in the database: path, size, and every byte consumed"


def test_only_the_new_bytes_of_a_grown_file_are_read(env):
    a = _write(env.root, "a", [_line(i) for i in range(500)])
    env.run()
    _append(a, [_line(i) for i in range(500, 503)])
    grown = env.run()
    assert grown["rows_in"] == 3 and grown["files_grown"] == 1 and grown["files_read"] == 1
    assert grown["bytes_read"] == sum(len(_line(i)) + 1 for i in range(500, 503))
    assert env.count() == 503


def test_a_file_landing_after_a_pass_is_read_by_the_next(env):
    _write(env.root, "a", [_line(i) for i in range(100)])
    env.run()
    _write(env.root, "late", [_line(i, ch="T3") for i in range(7)], day="2026-10-02")
    nxt = env.run()
    assert nxt["rows_in"] == 7 and nxt["files_new"] == 1 and nxt["files_skipped"] == 1
    assert env.count() == 107


def test_a_rewritten_file_is_noticed_and_read_again_from_the_start(env):
    _write(env.root, "a", [_line(i) for i in range(300)])
    env.run()
    # Shorter than before: the size alone says so.
    _write(env.root, "a", [_line(i, v=99.0) for i in range(200)])
    short = env.run()
    assert short["rows_in"] == 200 and short["files_rewritten"] == 1
    # The same 200 readings (row_hash is the identity) with a different value:
    # noticed, re-read, and idempotent, because bronze is immutable by rule and
    # a rewrite is never used to change or delete what silver already holds.
    assert env.count() == 300
    # Different bytes, different readings: the head and the last line say so.
    _write(env.root, "a", [_line(i, ch="T9") for i in range(200)])
    again = env.run()
    assert again["rows_in"] == 200 and again["files_rewritten"] == 1
    assert env.count() == 500


def test_a_partial_trailing_line_waits_for_its_newline(env):
    tail = _line(10)
    a = _write(env.root, "a", [_line(i) for i in range(10)], partial=tail[:40])
    first = env.run()
    assert first["rows_in"] == 10
    assert env.marks()[str(a.relative_to(env.root))][1] < a.stat().st_size, "the partial line is not consumed"
    with a.open("a") as f:
        f.write(tail[40:] + "\n")
    second = env.run()
    assert second["rows_in"] == 1 and env.count() == 11


class _Interrupted(BaseException):
    """Not an Exception: a normalizer error is counted and skipped; this is
    the process dying."""


def test_an_interrupted_pass_resumes_from_its_last_checkpoint(env):
    _write(env.root, "a", [_line(i) for i in range(1000)])
    registry = NormalizerRegistry()
    seen = {"n": 0}

    def dying(rec):
        seen["n"] += 1
        if seen["n"] == 450:
            raise _Interrupted()
        yield from _normalize(rec)

    registry.register(SCHEMA_REF, dying)
    with pytest.raises(_Interrupted):
        env.run(registry=registry, checkpoint_rows=200)
    # Two checkpoints committed rows and watermarks together; the rest rolled back.
    assert env.count() == 400
    assert env.marks()[f"{CONNECTOR}/_rows/2026-10-01/a.jsonl"][1] == sum(len(_line(i)) + 1 for i in range(400))
    resumed = env.run(checkpoint_rows=200)
    assert resumed["rows_in"] == 600 and env.count() == 1000


def test_memory_stays_bounded_on_a_large_file(env):
    n = 300_000
    _write(env.root, "big", [_line(i) for i in range(n)])
    tracemalloc.start()
    try:
        stats = env.run(checkpoint_rows=20_000)
        _current, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert stats["rows_in"] == n and env.count() == n
    assert peak < 48 * 2**20, f"peak {peak / 2**20:.0f} MiB: the file was not streamed"


def test_watermarks_are_held_one_directory_at_a_time(env, monkeypatch):
    """On the node this is for, one connector lands about 275,000 files a day
    under ``_rows/<day>/``. Loading every watermark of the root at the start
    of a pass would be millions of rows in memory within weeks, which is the
    failure class this module exists to remove. One directory's watermarks
    are loaded while that directory is walked, then dropped."""
    from axiom.extensions.builtins.data_platform.conformance.watermarks import FileWatermarks

    days = ["2026-10-01", "2026-10-02", "2026-10-03"]
    for d in days:
        for k in range(40):
            _write(env.root, f"f{k:03d}", [_line(k * 10 + i, day=d) for i in range(10)], day=d)
    first = env.run()
    assert first["rows_in"] == 1200 and first["files_seen"] == 120
    peak = {"loads": 0, "marks": 0}
    original = FileWatermarks.enter_dir

    def spy(self, directory):
        original(self, directory)
        peak["loads"] += 1
        peak["marks"] = max(peak["marks"], len(self._marks))

    monkeypatch.setattr(FileWatermarks, "enter_dir", spy)
    second = env.run()
    assert second["bytes_read"] == 0 and second["files_skipped"] == 120
    assert peak["loads"] == len(days), "one load per directory, not one per file"
    assert peak["marks"] == 40, "never more than one directory's watermarks in memory"


def test_rebuild_reads_everything_again_and_says_so(env):
    _write(env.root, "a", [_line(i) for i in range(300)])
    _write(env.root, "b", [_line(i, ch="T2") for i in range(300)])
    env.run()
    assert env.run()["rows_in"] == 0
    rebuilt = env.run(rebuild=True)
    assert rebuilt["rows_in"] == 600 and rebuilt["rebuilt"] is True and rebuilt["rederive"] is True
    assert env.count() == 600
    assert env.run()["rows_in"] == 0, "the rebuild re-seeded the watermarks"


def test_a_full_pass_without_watermarks_reads_everything_every_time(env):
    """Negative control: the old behaviour is still there, explicitly."""
    _write(env.root, "a", [_line(i) for i in range(100)])
    assert env.run(incremental=False)["rows_in"] == 100
    again = env.run(incremental=False)
    assert again["rows_in"] == 100 and again["incremental"] is False
    assert env.marks() == {}


def test_a_dry_run_advances_no_watermark(env):
    _write(env.root, "a", [_line(i) for i in range(50)])
    dry = env.run(apply=False)
    assert dry["rows_in"] == 50 and env.count() == 0 and env.marks() == {}
    assert env.run()["rows_in"] == 50
