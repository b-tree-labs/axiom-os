# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""One batch from a source this node does not know never stops every other source.

An edge serves many sources and the downstream pulls them through one cursor.
When a batch names a source with no local connector, the pull used to stop
there, so a single test batch, or one site registered on the edge before it was
registered downstream, held back every other site's data.

What has to hold:

- the other sources keep landing, and the cursor moves past the unknown batch;
- the unknown batch is not lost: its verified bytes are kept beside the cursor,
  because the edge may delete a batch once the cursor has passed it;
- once the source is registered, the next pull lands the held batch, once;
- the report names the held sources, so nobody has to find them in a log.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

from ...sources.edge import EdgePuller, FileCursor


def _rec(seq: int, source: str) -> tuple[dict, bytes]:
    blob = json.dumps([{"ts": f"2026-11-09T00:00:{seq:02d}Z", "v": seq}]).encode()
    rec = {
        "seq": seq,
        "source": source,
        "item_id": f"{source}-{seq}",
        "schema_ref": "loop/rows-v1",
        "content_hash": hashlib.sha256(blob).hexdigest(),
    }
    return rec, blob


class FakeEdge:
    def __init__(self, items):
        self.items = items

    def records(self, after, limit):
        return [r for r, _ in self.items if r["seq"] > after][:limit]

    def content(self, source, content_hash):
        for r, b in self.items:
            if r["content_hash"] == content_hash:
                return b
        raise FileNotFoundError(content_hash)  # the edge pruned it


@dataclass
class Result:
    rows_in: int
    rows_landed: int
    rows_duplicate: int = 0
    errored: bool = False


class FakeSink:
    def __init__(self):
        self.landed: list[str] = []

    def ingest_rows(self, source, batches):
        n = 0
        for b in batches:
            self.landed.append(b.item_id)
            n += len(b.rows)
        return Result(rows_in=n, rows_landed=n)


def _sinks(known: set[str], sink: FakeSink):
    def sink_for(name):
        if name not in known:
            raise FileNotFoundError(f"no connector named {name!r}")
        return sink

    return sink_for


def test_an_unknown_source_is_held_and_the_rest_keep_landing(tmp_path):
    items = [_rec(1, "site-a"), _rec(2, "probe"), _rec(3, "site-b")]
    sink = FakeSink()
    cursor = FileCursor(tmp_path / "cursor.json")
    report = EdgePuller(
        FakeEdge(items), sink_for=_sinks({"site-a", "site-b"}, sink), cursor=cursor
    ).pull()

    assert sink.landed == ["site-a-1", "site-b-3"]
    assert cursor.get() == 3
    assert report.held == {"probe": 1}
    assert [r["item_id"] for r, _ in cursor.held()] == ["probe-2"]


def test_a_held_batch_lands_once_after_its_source_is_registered(tmp_path):
    items = [_rec(1, "site-a"), _rec(2, "probe")]
    sink = FakeSink()
    cursor = FileCursor(tmp_path / "cursor.json")
    EdgePuller(FakeEdge(items), sink_for=_sinks({"site-a"}, sink), cursor=cursor).pull()

    # The edge has pruned everything the cursor passed; the held copy is all there is.
    report = EdgePuller(
        FakeEdge([]), sink_for=_sinks({"site-a", "probe"}, sink), cursor=cursor
    ).pull()
    assert sink.landed == ["site-a-1", "probe-2"]
    assert report.released == 1 and report.held == {}
    assert cursor.held() == []

    EdgePuller(FakeEdge([]), sink_for=_sinks({"site-a", "probe"}, sink), cursor=cursor).pull()
    assert sink.landed == ["site-a-1", "probe-2"]


def test_a_cursor_written_before_held_batches_existed_still_reads(tmp_path):
    path = tmp_path / "cursor.json"
    path.write_text(json.dumps({"after": 7}))
    cursor = FileCursor(path)
    assert cursor.get() == 7
    assert cursor.held() == []
