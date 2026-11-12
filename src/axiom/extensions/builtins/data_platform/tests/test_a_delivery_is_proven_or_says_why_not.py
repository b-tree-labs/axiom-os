# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""A delivery is proven stage by stage, and a check that did not run is not a pass.

Every stage had been reporting on itself: the pull said it pulled, a receipt said
landed, freshness said stale for so long nobody read it. These hold the comparison
between the stages, and above all that nothing passes by default.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

from axiom.extensions.builtins.data_platform import delivery_proof as dp

NOW = datetime(2026, 11, 7, 12, 0, tzinfo=UTC)
FIRST = datetime(2026, 11, 2, 10, 0, tzinfo=UTC)
LAST = datetime(2026, 11, 3, 10, 0, tzinfo=UTC)


class _Gold:
    """gold.deposit_status and a count of gold.signals, as a cursor would serve them."""

    def __init__(self, rows=(), served=100, broken=False):
        self.rows, self.served, self.broken = list(rows), served, broken
        self._last = None

    def execute(self, sql, params=None):
        if self.broken and ("gold.signals" in sql or "deposit_status" in sql):
            raise TimeoutError("statement timeout")
        self._last = sql

    def fetchall(self):
        return self.rows

    def fetchone(self):
        return (self.served,)


def _connector(root: Path, **params):
    return SimpleNamespace(
        name="loop-share",
        site="example-loop",
        bronze_root=str(root),
        params={"schema_ref": "loop/archive-v1", **params},
    )


def _sha(name: str) -> str:
    return hashlib.sha256(name.encode()).hexdigest()


def _file(root: Path, name: str, size: int = 10) -> str:
    sha = _sha(name)
    content = root / "_content" / sha[:2] / sha
    content.parent.mkdir(parents=True, exist_ok=True)
    content.write_bytes(b"x" * size)
    day = root / "loop-share" / "_records" / "2026-11-03"
    day.mkdir(parents=True, exist_ok=True)
    (day / f"{name}.json").write_text(
        json.dumps(
            {
                "item_id": name,
                "display_name": name,
                "content_sha256": sha,
                "size": size,
                "fetched_at": "2026-11-03T00:00:00+00:00",
            }
        ),
        encoding="utf-8",
    )
    return sha


def _listing(root: Path, *names: str, taken: datetime = NOW, size: int = 10) -> None:
    items = [{"id": n, "name": n, "bytes": size} for n in names]
    dp.write_source_manifest(root, "loop-share", items, now=taken)


def _landed(name, readings=100, first=FIRST, last=LAST):
    return (_sha(name), name, "landed", None, readings, first, last)


def _prove(root, cur, **params):
    return dp.prove_connector(cur, _connector(root, **params), now=NOW)


def test_every_stage_agreeing_is_proven(tmp_path):
    _file(tmp_path, "day.bz2")
    _listing(tmp_path, "day.bz2")
    proof = _prove(tmp_path, _Gold([_landed("day.bz2")], served=100))
    assert proof.verdict == dp.PROVEN, proof.reasons
    assert (proof.landed, proof.readings, proof.gold_checked) == (1, 100, 1)
    assert proof.newest_reading == LAST.isoformat()


def test_overlapping_exports_can_serve_more_than_a_file_gave(tmp_path):
    _file(tmp_path, "day.bz2")
    _listing(tmp_path, "day.bz2")
    assert _prove(tmp_path, _Gold([_landed("day.bz2")], served=500)).verdict == dp.PROVEN


def test_gold_holding_less_than_a_file_gave_is_named(tmp_path):
    _file(tmp_path, "day.bz2")
    _listing(tmp_path, "day.bz2")
    proof = _prove(tmp_path, _Gold([_landed("day.bz2")], served=99))
    assert proof.verdict == dp.GOLD_SHORT
    assert "99 of 100" in proof.reasons[0]


def test_a_file_the_source_holds_and_bronze_does_not_is_a_gap(tmp_path):
    _file(tmp_path, "day.bz2")
    _listing(tmp_path, "day.bz2", "tomorrow.bz2")
    proof = _prove(tmp_path, _Gold([_landed("day.bz2")]))
    assert proof.verdict == dp.SOURCE_GAP
    assert "tomorrow.bz2" in proof.reasons[0]


def test_a_changed_size_is_a_gap(tmp_path):
    _file(tmp_path, "day.bz2", size=10)
    _listing(tmp_path, "day.bz2", size=11)
    assert _prove(tmp_path, _Gold([_landed("day.bz2")])).verdict == dp.SOURCE_GAP


def test_an_unread_file_is_waiting_however_recent_the_others(tmp_path):
    _file(tmp_path, "day.bz2")
    _file(tmp_path, "next.bz2")
    _listing(tmp_path, "day.bz2", "next.bz2")
    proof = _prove(tmp_path, _Gold([_landed("day.bz2")]))
    assert proof.verdict == dp.WAITING and proof.unread == 1


def test_files_with_nothing_landed_cannot_pass_on_declines_alone(tmp_path):
    _file(tmp_path, "notes.txt")
    _listing(tmp_path, "notes.txt")
    row = (_sha("notes.txt"), "notes.txt", "declined", "a note", 0, None, None)
    assert _prove(tmp_path, _Gold([row])).verdict == dp.WAITING


def test_a_failed_file_beats_everything_else(tmp_path):
    _file(tmp_path, "bad.bz2")
    _listing(tmp_path, "bad.bz2")
    row = (_sha("bad.bz2"), "bad.bz2", "failed", "ValueError: line 3", 0, None, None)
    proof = _prove(tmp_path, _Gold([row]), held="waiting on a decision")
    assert proof.verdict == dp.FAILED
    assert "line 3" in " ".join(proof.reasons)


def test_a_held_connector_is_named_even_when_everything_else_agrees(tmp_path):
    _file(tmp_path, "day.bz2")
    _listing(tmp_path, "day.bz2")
    proof = _prove(tmp_path, _Gold([_landed("day.bz2")]), held="overlap decision pending")
    assert proof.verdict == dp.HELD
    assert "overlap" in proof.reasons[0]


def test_bytes_gone_from_bronze_fail(tmp_path):
    sha = _file(tmp_path, "day.bz2")
    (tmp_path / "_content" / sha[:2] / sha).unlink()
    _listing(tmp_path, "day.bz2")
    assert _prove(tmp_path, _Gold([_landed("day.bz2")])).verdict == dp.FAILED


# --- nothing passes by default -------------------------------------------------


def test_no_source_listing_is_never_a_pass(tmp_path):
    _file(tmp_path, "day.bz2")
    assert _prove(tmp_path, _Gold([_landed("day.bz2")])).verdict == dp.UNVERIFIED


def test_a_stale_listing_is_never_a_pass(tmp_path):
    _file(tmp_path, "day.bz2")
    _listing(tmp_path, "day.bz2", taken=NOW - timedelta(days=3))
    assert _prove(tmp_path, _Gold([_landed("day.bz2")])).verdict == dp.UNVERIFIED


def test_a_query_that_did_not_finish_is_never_a_pass(tmp_path):
    _file(tmp_path, "day.bz2")
    _listing(tmp_path, "day.bz2")
    assert _prove(tmp_path, _Gold(broken=True)).verdict == dp.UNVERIFIED


def test_a_count_that_timed_out_is_never_a_pass(tmp_path):
    _file(tmp_path, "day.bz2")
    _listing(tmp_path, "day.bz2")

    class _Slow(_Gold):
        def execute(self, sql, params=None):
            if "gold.signals" in sql:
                raise TimeoutError("statement timeout")

    assert _prove(tmp_path, _Slow([_landed("day.bz2")])).verdict == dp.UNVERIFIED


def test_history_that_cannot_be_counted_is_reported_beside_the_verdict(tmp_path):
    _file(tmp_path, "old.csv")
    _file(tmp_path, "day.bz2")
    _listing(tmp_path, "old.csv", "day.bz2")
    wide = _landed("old.csv", first=datetime(2004, 1, 10, tzinfo=UTC))
    proof = _prove(tmp_path, _Gold([wide, _landed("day.bz2")]))
    assert proof.verdict == dp.PROVEN
    assert (proof.gold_checked, proof.history_unchecked) == (1, 1)


# --- a site reads as its worst connector ----------------------------------------


def test_a_site_reads_as_the_worst_of_its_connectors(tmp_path, monkeypatch):
    _file(tmp_path, "day.bz2")
    _listing(tmp_path, "day.bz2")
    connector = _connector(tmp_path, held="stopped")
    report = dp.prove(_Gold([_landed("day.bz2")]), [connector], now=NOW)
    assert report["sites"] == {"example-loop": dp.HELD}
    assert report["proven"] is False


def test_no_connectors_proves_nothing():
    assert dp.prove(_Gold(), [], now=NOW)["proven"] is False


def test_a_document_mirror_is_listed_apart(tmp_path):
    _file(tmp_path, "paper.pdf")
    mirror = SimpleNamespace(name="loop-share", site=None, bronze_root=str(tmp_path), params={})
    report = dp.prove(_Gold(), [mirror], now=NOW)
    assert report["document_mirrors"] == ["loop-share"]
    assert report["sites"] == {}


def test_a_hidden_file_in_the_share_is_not_a_gap(tmp_path):
    _file(tmp_path, "day.bz2")
    _listing(tmp_path, "day.bz2", ".DS_Store")
    assert _prove(tmp_path, _Gold([_landed("day.bz2")])).verdict == dp.PROVEN


def _rows(monkeypatch, *, total=100, conformed=100, age=10.0):
    from axiom.extensions.builtins.data_platform import pipeline_proof

    monkeypatch.setattr(pipeline_proof, "_bronze_files", lambda r, c: (total, 1.0, ""))
    monkeypatch.setattr(pipeline_proof, "_watermarks", lambda cur, r, c: (conformed, age))


def _edge(root, **params):
    return SimpleNamespace(
        name="edge", site="example-reactor", bronze_root=str(root), params=params
    )


def test_a_rows_feed_with_everything_read_and_in_gold_is_proven(tmp_path, monkeypatch):
    _rows(monkeypatch)
    proof = dp.prove_rows(_Gold(served=LAST), _edge(tmp_path))
    assert proof.verdict == dp.PROVEN
    assert proof.newest_reading == LAST.isoformat()


def test_a_rows_feed_draining_normally_is_proven(tmp_path, monkeypatch):
    _rows(monkeypatch, conformed=90, age=30.0)
    assert dp.prove_rows(_Gold(served=LAST), _edge(tmp_path)).verdict == dp.PROVEN


def test_a_rows_feed_that_stopped_advancing_is_not(tmp_path, monkeypatch):
    _rows(monkeypatch, conformed=90, age=99999.0)
    assert dp.prove_rows(_Gold(served=LAST), _edge(tmp_path)).verdict == dp.WAITING


def test_rows_in_bronze_and_nothing_in_gold_is_short(tmp_path, monkeypatch):
    _rows(monkeypatch)
    assert dp.prove_rows(_Gold(served=None), _edge(tmp_path)).verdict == dp.GOLD_SHORT


def test_a_rows_read_that_did_not_finish_is_not_a_pass(tmp_path, monkeypatch):
    _rows(monkeypatch)

    class _Slow(_Gold):
        def execute(self, sql, params=None):
            raise TimeoutError("statement timeout")

    assert dp.prove_rows(_Slow(), _edge(tmp_path)).verdict == dp.UNVERIFIED


def test_a_held_rows_connector_does_not_read_as_flowing(tmp_path, monkeypatch):
    _rows(monkeypatch)
    edge = _edge(tmp_path, held="stopped")
    assert dp.prove(_Gold(served=LAST), [edge], now=NOW)["sites"] == {"example-reactor": dp.HELD}


def test_a_site_needs_all_of_its_connectors(tmp_path, monkeypatch):
    _rows(monkeypatch, conformed=90, age=99999.0)
    report = dp.prove(_Gold(served=LAST), [_edge(tmp_path)], now=NOW)
    assert report["proven"] is False


def test_a_rows_connector_that_never_delivered_is_listed_apart(tmp_path, monkeypatch):
    _file(tmp_path, "day.bz2")
    _listing(tmp_path, "day.bz2")
    _rows(monkeypatch, total=0, conformed=0)
    gold = _Gold([_landed("day.bz2")], served=100)
    report = dp.prove(gold, [_connector(tmp_path), _edge(tmp_path)], now=NOW)
    assert report["idle_connectors"] == ["edge"]
    assert "example-reactor" not in report["sites"]
    assert report["sites"] == {"example-loop": dp.PROVEN}


def test_only_idle_connectors_prove_nothing(tmp_path, monkeypatch):
    _rows(monkeypatch, total=0, conformed=0)
    assert dp.prove(_Gold(), [_edge(tmp_path)], now=NOW)["proven"] is False
