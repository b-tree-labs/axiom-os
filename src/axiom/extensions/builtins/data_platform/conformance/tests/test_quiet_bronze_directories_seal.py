# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""A quiet bronze directory seals: one row replaces its per-file watermarks.

One watermark per bronze file, never pruned, grew to 20M rows on one node
(about 275,000 new a day). A directory whose files are all consumed and which
has been untouched for ``seal_after_days`` is sealed: one row in
``conform_sealed_dirs``, its per-file rows deleted, and the walker skips it
without stat-ing a single file in it, so pruning never makes a file look new.
A file added to a sealed directory is still read (the directory's mtime moves);
the files the seal covered are not read again.

Against a real TimescaleDB, like the incremental suite.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

import psycopg
import pytest

from axiom.extensions.builtins.data_platform.conformance.tests.test_conform_is_incremental import (
    _line,
    _write,
    env,  # noqa: F401 - fixture
)
from axiom.extensions.builtins.data_platform.conformance.watermarks import (
    SEALED_TABLE,
    WATERMARKS_TABLE,
)

OLD = time.time() - 30 * 86400


def _age(root: Path, day: str) -> Path:
    d = root / "loop" / "_rows" / day
    for f in d.iterdir():
        os.utime(f, (OLD, OLD))
    os.utime(d, (OLD, OLD))
    return d


@pytest.fixture
def clean(env):  # noqa: F811
    with psycopg.connect(env.dsn, autocommit=True) as c:
        c.execute(f"DROP TABLE IF EXISTS {SEALED_TABLE}")
    return env


def _rows(e, table):
    with psycopg.connect(e.dsn) as c:
        if c.execute("SELECT to_regclass(%s)", (table,)).fetchone()[0] is None:
            return 0
        return c.execute(f"SELECT count(*) FROM {table} WHERE root = %s", (str(e.root),)).fetchone()[0]


def test_an_old_consumed_directory_seals_and_is_never_read_again(clean, monkeypatch):
    for k in range(5):
        _write(clean.root, f"f{k}", [_line(k * 10 + i) for i in range(10)])
    _age(clean.root, "2026-10-01")
    first = clean.run()
    assert first["rows_in"] == 50 and first["dirs_sealed"] == 1
    assert _rows(clean, WATERMARKS_TABLE) == 0, "per-file rows are deleted"
    assert _rows(clean, SEALED_TABLE) == 1

    stat_calls = {"n": 0}
    real = Path.stat

    def counting(self, *a, **kw):
        if self.suffix == ".jsonl":
            stat_calls["n"] += 1
        return real(self, *a, **kw)

    monkeypatch.setattr(Path, "stat", counting)
    second = clean.run()
    assert second["rows_in"] == 0 and second["files_seen"] == 0 and second["dirs_skipped_sealed"] == 1
    assert stat_calls["n"] == 0, "a sealed directory's files are not stat-ed"
    assert clean.count() == 50


def test_a_recent_directory_does_not_seal(clean):
    _write(clean.root, "a", [_line(i) for i in range(10)])
    first = clean.run()
    assert first["dirs_sealed"] == 0 and _rows(clean, WATERMARKS_TABLE) == 1


def test_a_partly_consumed_directory_does_not_seal(clean):
    _write(clean.root, "a", [_line(i) for i in range(10)], partial=_line(10)[:30])
    _age(clean.root, "2026-10-01")
    first = clean.run()
    assert first["dirs_sealed"] == 0 and _rows(clean, SEALED_TABLE) == 0


def test_a_file_added_to_a_sealed_directory_is_read_and_the_rest_are_not(clean, monkeypatch):
    for k in range(3):
        _write(clean.root, f"f{k}", [_line(k * 10 + i) for i in range(10)])
    _age(clean.root, "2026-10-01")
    clean.run()
    _write(clean.root, "late", [_line(100 + i) for i in range(4)])  # moves the dir mtime
    nxt = clean.run()
    assert nxt["rows_in"] == 4, "only the new file is read; the sealed ones are not re-read from byte 0"
    assert clean.count() == 34
    # Once quiet again, it reseals, covering all four files. (A short seal
    # age instead of back-dating: back-dating would change the new file's
    # mtime under its watermark, which a real file never does.)
    monkeypatch.setenv("AXIOM_CONFORM_SEAL_AFTER_DAYS", str(1 / 86400))
    time.sleep(1.5)
    again = clean.run()
    assert again["rows_in"] == 0 and again["dirs_sealed"] == 1 and _rows(clean, WATERMARKS_TABLE) == 0
    with psycopg.connect(clean.dsn) as c:
        assert c.execute(f"SELECT files FROM {SEALED_TABLE}").fetchone()[0] == 4


def test_sealing_can_be_turned_off(clean, monkeypatch):
    monkeypatch.setenv("AXIOM_CONFORM_SEAL_AFTER_DAYS", "0")
    _write(clean.root, "a", [_line(i) for i in range(10)])
    _age(clean.root, "2026-10-01")
    assert clean.run()["dirs_sealed"] == 0
