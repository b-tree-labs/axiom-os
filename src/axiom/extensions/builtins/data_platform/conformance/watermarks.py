# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""A durable watermark per bronze file, so a conform pass reads only new bytes.

Bronze files are append-only by contract (one JSON record per line, landed by
the ingest sink), and most of them never change again. A pass that re-reads
all of them every time does not scale with the archive. The watermark says,
per file, how many bytes have been conformed and committed, and two small
hashes that notice a file that was rewritten rather than grown: the first
``HEAD_BYTES`` of the file (never past the offset), and the last complete
line consumed. Checking them costs one open and two short reads; a file whose
size and mtime are unchanged and whose watermark reaches its end is not
opened at all.

The watermarks live in the database, in this extension's own schema
(ADR-052), and are written **in the same transaction** as the rows they
account for: the runner persists them at every checkpoint, just before the
commit. A pass cut short therefore resumes exactly at its last commit, and a
row is never counted as conformed before it is.

**One directory at a time.** On the node this is for, one connector lands
about 275,000 files a day under ``_rows/<day>/``. Loading every watermark of
a root at the start of a pass would be millions of rows in memory within
weeks, which is the failure class this module exists to remove. So the
walker calls :meth:`FileWatermarks.enter_dir` as it enters each dated
directory: that directory's watermarks are loaded (one query, by the
indexed ``(root, dir)``), consulted while it is walked, and dropped when the
walker moves on. Memory is bounded by one directory's files.

A rewritten file is read again from the start. Bronze being immutable by
rule, the rewrite is noticed and reported, never used to delete anything an
earlier version put in silver; the upsert is idempotent, so re-reading is
safe. A trailing line without its newline is a record still being written
and is left for the next pass.
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

WATERMARKS_SCHEMA = "data_platform"
WATERMARKS_TABLE = f"{WATERMARKS_SCHEMA}.conform_watermarks"

#: How much of a file's head is hashed to notice a rewrite.
HEAD_BYTES = 4096

_DDL = (
    f"CREATE SCHEMA IF NOT EXISTS {WATERMARKS_SCHEMA}",
    f"""CREATE TABLE IF NOT EXISTS {WATERMARKS_TABLE} (
        root text NOT NULL,
        path text NOT NULL,
        dir text NOT NULL,
        size bigint NOT NULL,
        offset_bytes bigint NOT NULL,
        mtime_ns bigint NOT NULL,
        head_hash text NOT NULL,
        head_len integer NOT NULL DEFAULT 0,
        tail_hash text NOT NULL,
        tail_len integer NOT NULL,
        rows_read bigint NOT NULL DEFAULT 0,
        updated_at timestamptz NOT NULL DEFAULT now(),
        PRIMARY KEY (root, path)
    )""",
    f"CREATE INDEX IF NOT EXISTS conform_watermarks_dir ON {WATERMARKS_TABLE} (root, dir)",
)

_COLUMNS = "path, size, offset_bytes, mtime_ns, head_hash, tail_hash, tail_len, rows_read, head_len"


def ensure_schema(cur) -> None:
    for stmt in _DDL:
        cur.execute(stmt)


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@dataclass
class Watermark:
    path: str
    size: int
    offset: int
    mtime_ns: int
    head_hash: str
    tail_hash: str
    tail_len: int
    rows_read: int = 0
    #: How many leading bytes ``head_hash`` covers: at most ``HEAD_BYTES``,
    #: and never past ``offset``, so a trailing line still being written is
    #: not part of what a later pass expects to find unchanged.
    head_len: int = HEAD_BYTES

    @property
    def dir(self) -> str:
        return str(Path(self.path).parent)


@dataclass
class Plan:
    """Where to start reading one file, and why."""

    start: int
    reason: str  # new | grown | rewritten | unchanged


class FileWatermarks:
    """The watermarks of one bronze root, one directory at a time."""

    def __init__(self, cur, root: Path | str) -> None:
        self.root = Path(root)
        self._cur = cur
        self._dir: str | None = None
        self._marks: dict[str, Watermark] = {}
        self._pending: dict[str, Watermark] = {}
        #: Per file this pass: the rows already accounted for before it
        #: (continuing a grown file), and the bytes reported so far, so that
        #: :meth:`advance` may be called at every checkpoint with cumulative
        #: figures and count each byte once. Both are scoped to the current
        #: directory and dropped with it.
        self._base_rows: dict[str, int] = {}
        self._reported: dict[str, int] = {}
        self.counts: dict[str, int] = {
            "files_new": 0, "files_grown": 0, "files_rewritten": 0, "files_skipped": 0, "bytes_read": 0,
        }
        #: How many directories were loaded this pass (a test reads it).
        self.loads = 0
        ensure_schema(cur)

    @classmethod
    def reset(cls, cur, root: Path | str) -> int:
        """Forget every watermark of a root (a rebuild). Returns how many."""
        ensure_schema(cur)
        cur.execute(f"DELETE FROM {WATERMARKS_TABLE} WHERE root = %s", (str(Path(root)),))
        return int(cur.rowcount or 0)

    def key(self, path: Path) -> str:
        return str(Path(path).relative_to(self.root))

    def enter_dir(self, directory: Path | str) -> None:
        """Load one directory's watermarks and drop the previous directory's.

        Pending watermarks are untouched: they belong to the transaction, not
        to the directory, and :meth:`persist` writes them whichever directory
        the walker is in by then.
        """
        rel = self.key(Path(directory))
        if rel == self._dir:
            return
        self._marks.clear()
        self._base_rows.clear()
        self._reported.clear()
        self._cur.execute(
            f"SELECT {_COLUMNS} FROM {WATERMARKS_TABLE} WHERE root = %s AND dir = %s",
            (str(self.root), rel),
        )
        for path, size, offset, mtime_ns, head, tail, tail_len, rows, head_len in self._cur.fetchall():
            self._marks[path] = Watermark(path, int(size), int(offset), int(mtime_ns), head, tail,
                                          int(tail_len), int(rows), int(head_len))
        self._dir = rel
        self.loads += 1

    def plan(self, path: Path, stat: os.stat_result) -> Plan:
        """Decide where to read ``path`` from. Opens the file only when its size
        or mtime changed, and then reads at most the head and the last line.
        The file's directory must have been entered."""
        key = self.key(path)
        mark = self._marks.get(key)
        if mark is None:
            self.counts["files_new"] += 1
            return Plan(0, "new")
        fully_consumed = mark.offset >= mark.size
        if fully_consumed and stat.st_size == mark.size and stat.st_mtime_ns == mark.mtime_ns:
            # The common case, and the only one that opens nothing: every
            # byte was conformed and the file has not been touched since. A
            # watermark that sits mid-file (a pass cut short, a trailing
            # partial line) never takes this shortcut, or a resumed pass
            # would skip what it stopped on.
            self.counts["files_skipped"] += 1
            return Plan(mark.offset, "unchanged")
        if stat.st_size < mark.offset or not self._intact(path, mark):
            self.counts["files_rewritten"] += 1
            return Plan(0, "rewritten")
        if mark.offset >= stat.st_size:
            # Touched but not grown, and the head and tail agree: nothing new.
            self.counts["files_skipped"] += 1
            return Plan(mark.offset, "unchanged")
        self.counts["files_grown"] += 1
        self._base_rows[key] = mark.rows_read
        return Plan(mark.offset, "grown")

    @staticmethod
    def _intact(path: Path, mark: Watermark) -> bool:
        """The head and the last consumed line still read as they did."""
        with path.open("rb") as f:
            if _sha(f.read(mark.head_len)) != mark.head_hash:
                return False
            if mark.tail_len:
                f.seek(mark.offset - mark.tail_len)
                if _sha(f.read(mark.tail_len)) != mark.tail_hash:
                    return False
        return True

    def advance(self, path: Path, *, offset: int, stat: os.stat_result, head: bytes,
                last_line: bytes, rows_read: int, bytes_read: int) -> None:
        """Record what was consumed from ``path``, pending the next persist.

        ``rows_read`` and ``bytes_read`` are cumulative for this pass, so the
        walker may call this at every checkpoint and once more at the end.
        """
        key = self.key(path)
        reported = self._reported.get(key, 0)
        self.counts["bytes_read"] += max(bytes_read - reported, 0)
        self._reported[key] = bytes_read
        # Never past what was consumed: a trailing line still being written
        # is not part of what the next pass expects to find unchanged.
        head_len = min(len(head), offset)
        self._pending[key] = Watermark(
            path=key, size=stat.st_size, offset=offset, mtime_ns=stat.st_mtime_ns,
            head_hash=_sha(head[:head_len]), tail_hash=_sha(last_line), tail_len=len(last_line),
            rows_read=self._base_rows.get(key, 0) + rows_read, head_len=head_len,
        )

    def persist(self, cur=None) -> int:
        """Write every pending watermark. The caller commits. Returns how many."""
        cur = self._cur if cur is None else cur
        pending, self._pending = self._pending, {}
        for mark in pending.values():
            cur.execute(
                f"INSERT INTO {WATERMARKS_TABLE} (root, path, dir, size, offset_bytes, mtime_ns, head_hash, "
                "head_len, tail_hash, tail_len, rows_read, updated_at) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, now()) "
                "ON CONFLICT (root, path) DO UPDATE SET dir = EXCLUDED.dir, size = EXCLUDED.size, "
                "offset_bytes = EXCLUDED.offset_bytes, mtime_ns = EXCLUDED.mtime_ns, "
                "head_hash = EXCLUDED.head_hash, head_len = EXCLUDED.head_len, tail_hash = EXCLUDED.tail_hash, "
                "tail_len = EXCLUDED.tail_len, rows_read = EXCLUDED.rows_read, updated_at = now()",
                (str(self.root), mark.path, mark.dir, mark.size, mark.offset, mark.mtime_ns, mark.head_hash,
                 mark.head_len, mark.tail_hash, mark.tail_len, mark.rows_read),
            )
            if mark.dir == self._dir:
                self._marks[mark.path] = mark
        return len(pending)

    def discard_pending(self) -> None:
        """Forget what has not been persisted (a rolled-back checkpoint)."""
        self._pending = {}

    def stats(self) -> dict[str, Any]:
        return dict(self.counts)


def read_lines(path: Path, start: int):
    """Yield ``(line_bytes, end_offset)`` for every complete line from
    ``start``. A trailing line without its newline is not yielded: it is a
    record still being written. Streams; never holds the file in memory."""
    with path.open("rb") as f:
        f.seek(start)
        offset = start
        for line in f:
            if not line.endswith(b"\n"):
                break
            offset += len(line)
            yield line, offset


def head_of(path: Path) -> bytes:
    with path.open("rb") as f:
        return f.read(HEAD_BYTES)


__all__ = [
    "HEAD_BYTES",
    "WATERMARKS_SCHEMA",
    "WATERMARKS_TABLE",
    "FileWatermarks",
    "Plan",
    "Watermark",
    "ensure_schema",
    "head_of",
    "read_lines",
]
