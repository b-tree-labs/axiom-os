# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Join per-shard pytest-testmon maps into the one map a full run would write (#2441 item 9).

A sharded full run leaves one ``.testmondata`` per shard, each holding its shard's
tests. A pull request selects against a single restored map, so main must save one
that holds every test. The tables are small and plain (an environment, the tests, the
file fingerprints and the links between them), so the join is a rewrite of ids, not a
reinterpretation: tests copy over, fingerprints are shared where the file content is
the same, links follow.

It refuses, writing nothing, whenever the join could be wrong: an input missing or not
a testmon database, a different testmon data version or table layout, a different
environment (the interpreter or package fingerprint), a table it does not know how to
join holding rows, or the same test in two maps (shards that overlap). The caller then
keeps the previous map, which only costs selection precision, never a skipped test.

Stdlib only. ``tests/test_testmon_merge`` runs a real testmon whole and as shards and
requires the join to equal the whole, including the tests selected after an edit.
"""

from __future__ import annotations

import os
import sqlite3
import sys
from collections.abc import Sequence
from pathlib import Path

_JOINED = ("environment", "file_fp", "test_execution", "test_execution_file_fp")
#: Tables that carry no rows to join in the layout this was written against.
_MUST_BE_EMPTY = ("suite_execution_file_fsha",)


class MergeRefused(Exception):
    """The maps cannot be joined safely; nothing was written."""


def _open(path: Path) -> sqlite3.Connection:
    if not path.is_file():
        raise MergeRefused(f"{path} is not a file")
    try:
        connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        connection.execute("SELECT count(*) FROM sqlite_master").fetchone()
        return connection
    except sqlite3.Error as error:
        raise MergeRefused(f"{path} is not a readable testmon map: {error}") from error


def _layout(connection: sqlite3.Connection) -> dict[str, str]:
    rows = connection.execute("SELECT name, sql FROM sqlite_master WHERE type = 'table'").fetchall()
    return {name: " ".join((sql or "").split()) for name, sql in rows}


def _environments(connection: sqlite3.Connection) -> frozenset[tuple]:
    return frozenset(
        connection.execute(
            "SELECT environment_name, system_packages, python_version FROM environment"
        ).fetchall()
    )


def _check(paths: Sequence[Path]) -> None:
    first_version = first_layout = first_env = None
    for index, path in enumerate(paths):
        connection = _open(path)
        try:
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            layout = _layout(connection)
            missing = [t for t in _JOINED if t not in layout]
            if missing:
                raise MergeRefused(f"{path} lacks the table(s) {missing}: not a layout this joins")
            for table in _MUST_BE_EMPTY:
                if (
                    table in layout
                    and connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
                ):
                    raise MergeRefused(
                        f"{path} has rows in {table}, which this does not know how to join"
                    )
            env = _environments(connection)
        except sqlite3.Error as error:
            raise MergeRefused(f"{path} is not a readable testmon map: {error}") from error
        finally:
            connection.close()
        if index == 0:
            first_version, first_layout, first_env = version, layout, env
            continue
        if version != first_version:
            raise MergeRefused(
                f"{path} is testmon data version {version}, the first is {first_version}"
            )
        if {t: layout[t] for t in _JOINED} != {t: first_layout[t] for t in _JOINED}:
            raise MergeRefused(f"{path} has a different table layout from the first map")
        if env != first_env:
            raise MergeRefused(
                f"{path} was written for a different environment (interpreter or package "
                "fingerprint) from the first map"
            )


def _columns(connection: sqlite3.Connection, table: str, skip: Sequence[str]) -> list[str]:
    return [
        row[1] for row in connection.execute(f"PRAGMA table_info({table})") if row[1] not in skip
    ]


def merge_maps(inputs: Sequence[Path | str], out: Path | str) -> None:
    """Write ``out`` as the join of ``inputs``, or raise :class:`MergeRefused` and write nothing."""
    paths = [Path(p) for p in inputs]
    out = Path(out)
    if not paths:
        raise MergeRefused("no maps to join")
    _check(paths)

    temporary = out.with_name(out.name + ".merging")
    temporary.unlink(missing_ok=True)
    try:
        source = _open(paths[0])
        target = sqlite3.connect(temporary)
        try:
            source.backup(target)
        finally:
            source.close()
        try:
            _join_rest(target, paths[1:])
            target.commit()
        finally:
            target.close()
        os.replace(temporary, out)
    except MergeRefused:
        temporary.unlink(missing_ok=True)
        raise
    except sqlite3.Error as error:
        temporary.unlink(missing_ok=True)
        raise MergeRefused(f"the join failed: {error}") from error
    finally:
        temporary.unlink(missing_ok=True)


def _join_rest(target: sqlite3.Connection, rest: Sequence[Path]) -> None:
    env_id = {
        (name, packages, version): row_id
        for row_id, name, packages, version in target.execute(
            "SELECT id, environment_name, system_packages, python_version FROM environment"
        )
    }
    held = {
        (env, name)
        for env, name in target.execute("SELECT environment_id, test_name FROM test_execution")
    }
    fp_cols = _columns(target, "file_fp", ("id",))
    te_cols = _columns(target, "test_execution", ("id",))
    fp_join = ", ".join(fp_cols)
    fp_marks = ", ".join("?" for _ in fp_cols)
    te_join = ", ".join(te_cols)
    te_marks = ", ".join("?" for _ in te_cols)
    fk_at = te_cols.index("environment_id")
    for path in rest:
        source = _open(path)
        try:
            their_env = {
                row_id: (name, packages, version)
                for row_id, name, packages, version in source.execute(
                    "SELECT id, environment_name, system_packages, python_version FROM environment"
                )
            }
            fp_map: dict[int, int] = {}
            for row in source.execute(f"SELECT id, {fp_join} FROM file_fp"):
                values = row[1:]
                fields = dict(zip(fp_cols, values, strict=True))
                existing = target.execute(
                    "SELECT id FROM file_fp WHERE filename = ? AND fsha = ? AND method_checksums IS ?",
                    (fields["filename"], fields["fsha"], fields["method_checksums"]),
                ).fetchone()
                if existing:
                    fp_map[row[0]] = existing[0]
                else:
                    cursor = target.execute(
                        f"INSERT INTO file_fp ({fp_join}) VALUES ({fp_marks})", values
                    )
                    fp_map[row[0]] = cursor.lastrowid
            te_map: dict[int, int] = {}
            for row in source.execute(f"SELECT id, {te_join} FROM test_execution"):
                values = list(row[1:])
                env = env_id[their_env[values[fk_at]]]
                name = values[te_cols.index("test_name")]
                if (env, name) in held:
                    raise MergeRefused(
                        f"{path} and an earlier map both hold {name}: shards overlap"
                    )
                held.add((env, name))
                values[fk_at] = env
                cursor = target.execute(
                    f"INSERT INTO test_execution ({te_join}) VALUES ({te_marks})", values
                )
                te_map[row[0]] = cursor.lastrowid
            for test_id, fp_id in source.execute(
                "SELECT test_execution_id, fingerprint_id FROM test_execution_file_fp"
            ):
                target.execute(
                    "INSERT INTO test_execution_file_fp (test_execution_id, fingerprint_id) VALUES (?, ?)",
                    (te_map[test_id], fp_map[fp_id]),
                )
        except KeyError as error:
            raise MergeRefused(f"{path} has a row pointing at nothing ({error})") from error
        finally:
            source.close()


def main(argv: Sequence[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) < 3 or args[0] != "--out":
        print("usage: testmon_merge --out OUTPUT INPUT [INPUT ...]", file=sys.stderr)
        return 2
    try:
        merge_maps(args[2:], args[1])
    except MergeRefused as error:
        print(f"::warning::testmon maps not joined, the previous map stays: {error}")
        return 1
    print(f"joined {len(args) - 2} testmon maps into {args[1]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
