# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""An edge's outbox costs the same per append at a million records as at ten.

Every append used to re-read the whole outbox to check for a duplicate, and
every page a downstream pulled re-scanned it from the first line. Measured on
2026-10-08: 2,000 appends took 8.5 s, the next 4,000 took 35 s. The outbox is
never trimmed, so a month of one site's ordinary traffic (86,400 one-second
batches a day) would have made each append take seconds: the edge slows to a
stop with no outage at all, and a reconnect flood finishes it.
"""

from __future__ import annotations

import time
import types

from axiom.extensions.builtins.data_platform.ingest_sink.edge import EdgeOutbox


def _batch(n_rows: int = 10):
    return types.SimpleNamespace(item_id="i", schema_ref="s", etag=None, source_path=None,
                                 metadata={}, rows=[0] * n_rows)


def _fill(outbox: EdgeOutbox, start: int, n: int) -> float:
    t = time.perf_counter()
    for i in range(start, start + n):
        outbox.record(source="src", batch=_batch(), content_hash=f"{i:064x}", rows_landed=10)
    return time.perf_counter() - t


def test_appending_never_reads_the_outbox_back(tmp_path):
    """Proved by structure, not by timing: once indexed, the outbox file can be
    made unreadable and appends (with their duplicate check) still work, so an
    append's cost cannot depend on how long the outbox is."""
    outbox = EdgeOutbox(tmp_path)
    _fill(outbox, 0, 600)
    (tmp_path / "outbox.jsonl").chmod(0o200)  # write-only: any read would fail
    try:
        assert outbox.record(source="src", batch=_batch(), content_hash=f"{5:064x}", rows_landed=10) is None
        assert outbox.record(source="src", batch=_batch(), content_hash=f"{9999:064x}",
                             rows_landed=10)["seq"] == 601
        assert EdgeOutbox(tmp_path).last_seq() == 601
    finally:
        (tmp_path / "outbox.jsonl").chmod(0o600)


def test_a_duplicate_is_still_refused_across_processes_and_restarts(tmp_path):
    a, b = EdgeOutbox(tmp_path), EdgeOutbox(tmp_path)
    assert a.record(source="src", batch=_batch(), content_hash="1" * 64, rows_landed=10)["seq"] == 1
    assert b.record(source="src", batch=_batch(), content_hash="1" * 64, rows_landed=10) is None
    assert b.record(source="src", batch=_batch(), content_hash="2" * 64, rows_landed=10)["seq"] == 2
    assert a.record(source="src", batch=_batch(), content_hash="3" * 64, rows_landed=10)["seq"] == 3
    # Same hash under another source is a different batch.
    assert a.record(source="other", batch=_batch(), content_hash="1" * 64, rows_landed=10)["seq"] == 4
    restarted = EdgeOutbox(tmp_path)
    assert restarted.record(source="src", batch=_batch(), content_hash="2" * 64, rows_landed=10) is None
    assert restarted.last_seq() == 4


def test_an_outbox_written_before_the_index_existed_is_still_deduplicated(tmp_path):
    import json

    lines = [json.dumps({"seq": i, "source": "src", "content_hash": f"{i:064x}", "item_id": "i",
                         "schema_ref": "s", "rows": 1, "rows_landed": 1}) for i in range(1, 6)]
    (tmp_path / "outbox.jsonl").write_text("\n".join(lines) + "\n")
    outbox = EdgeOutbox(tmp_path)
    assert outbox.record(source="src", batch=_batch(), content_hash=f"{3:064x}", rows_landed=1) is None
    assert outbox.record(source="src", batch=_batch(), content_hash=f"{9:064x}", rows_landed=1)["seq"] == 6


def test_a_page_after_a_late_cursor_seeks_past_the_start(tmp_path):
    """A late page never parses the early lines: with the first line made
    unparseable, a page near the end still reads, and a page from the start
    is the one that fails."""
    outbox = EdgeOutbox(tmp_path)
    _fill(outbox, 0, 1200)
    path = tmp_path / "outbox.jsonl"
    raw = path.read_bytes()
    first = raw.index(b"\n")
    path.write_bytes(b"x" * first + raw[first:])  # same length, so offsets still hold
    assert [r["seq"] for r in outbox.read(after=1190, limit=100)] == list(range(1191, 1201))
    assert [r["seq"] for r in outbox.read(after=1023, limit=3)] == [1024, 1025, 1026]
    import pytest

    with pytest.raises(ValueError):
        outbox.read(after=0, limit=3)
