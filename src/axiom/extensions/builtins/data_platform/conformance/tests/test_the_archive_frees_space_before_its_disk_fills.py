# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""A site's local archive frees space by retention before its disk fills (C-12).

The archive keeps bronze batches (with the outbox that orders them) and
``silver.signals`` in a TimescaleDB hypertable. Under pressure (here, over its
declared budget; on a node, also the disk alarm), ``data archive-reclaim``
rotates out the oldest data older than the declared minimum, oldest first and
only as much as clears the pressure, and never a batch the forwarder has not
yet delivered upstream.

Real pieces throughout: batches land through the real tabular ingest sink and
outbox, silver is a real compressed hypertable in a TimescaleDB container, and
the pass runs through the CLI verb the archive role's companion runs. Only the
clock at landing is moved back, so the old batches are old.
"""

from __future__ import annotations

import hashlib
import json
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from axiom.rag.ingest_router import Disposition

from ... import cli
from ...agents.plinth.connectors import ConnectorConfig, save_connector
from ...archive_space import ArchivePolicy, bronze_bytes, silver_bytes
from ...bronze import FilesystemTabularBronzeSink, TabularBronzeWriter
from ...conformance import pg_upsert
from ...conformance.timeseries import TimeseriesPolicy, make_signals_time_partitioned
from ...ingest_sink import PushRowBatch, TabularIngestSink
from ...ingest_sink import edge as edge_mod
from ...ingest_sink.edge import EdgeOutbox

SOURCE = "site-a-src"
NOW = datetime.now(UTC)
OLD_DAYS = [200, 199, 198, 197]  # four old silver chunks, oldest first


@pytest.fixture(autouse=True)
def _no_ambient_dsn(monkeypatch):
    # Never the developer's database: every pass here names its own DSN.
    for name in ("DP1_RAG_DSN", "DATABASE_URL", "AXIOM_DB_URL",
                 "AXIOM_ARCHIVE_KEEP_DAYS", "AXIOM_ARCHIVE_BUDGET_BYTES",
                 "AXIOM_ARCHIVE_CONTRIBUTOR", "AXIOM_EDGE_DOWNSTREAMS"):
        monkeypatch.delenv(name, raising=False)
    # The disk alarm floor is far below anything a test machine has free, so
    # the pressure in these tests is the declared budget alone.
    monkeypatch.setenv("AXIOM_DISK_ALARM_FREE_BYTES", "1")
    monkeypatch.setenv("AXIOM_DISK_ALARM_FREE_PERCENT", "0")
    monkeypatch.setenv("AXIOM_INGEST_MIN_FREE_BYTES", "1")
    monkeypatch.setenv("AXIOM_INGEST_MIN_FREE_PERCENT", "0")


def _land(sink, i: int, at: datetime, monkeypatch) -> None:
    class _Then(datetime):
        @classmethod
        def now(cls, tz=None):
            return at

    monkeypatch.setattr(edge_mod, "datetime", _Then)
    rows = [{"channel": "TC1", "ts": f"2026-01-01T00:00:{k:02d}Z", "value": 20.0 + k, "run": i}
            for k in range(200)]
    sink.ingest_rows(SOURCE, [PushRowBatch(item_id=f"run-{i}", schema_ref="loop/rows-v1",
                                           rows=rows, metadata={"site": "site-a"})])
    monkeypatch.setattr(edge_mod, "datetime", datetime)


def _silver_row(day: datetime, k: int) -> dict:
    ts = (day + timedelta(seconds=k)).isoformat()
    return {"site": "site-a", "feed": "site-a.live", "channel": "TC1", "ts": ts,
            "value": float(k), "unit": "degC", "quality": "ok", "source_class": "measured",
            "schema_ref": "site-a/v1",
            "row_hash": hashlib.sha256(f"{ts}|TC1".encode()).hexdigest()}


@pytest.fixture
def archive(tmp_path, conn, timescale_dsn, monkeypatch):
    """Five old batches (three delivered upstream), one new; four old silver days and today."""
    state = tmp_path / "state"
    monkeypatch.setenv("AXI_STATE_DIR", str(state))
    bronze = tmp_path / "bronze"
    outbox_dir = tmp_path / "outbox"
    save_connector(ConnectorConfig(name=SOURCE, kind="push", bronze_root=str(bronze), site="site-a"),
                   state_dir=state)
    outbox = EdgeOutbox(outbox_dir)
    sink = TabularIngestSink(
        writer=TabularBronzeWriter(rules=[], sink=FilesystemTabularBronzeSink(root=bronze),
                                   default_disposition=Disposition.ALLOW,
                                   default_tier="restricted"),
        on_landed=outbox.record,
    )
    for i in range(1, 6):
        _land(sink, i, NOW - timedelta(days=200), monkeypatch)
    _land(sink, 6, NOW, monkeypatch)
    assert outbox.last_seq() == 6
    # The forwarder has delivered 1-3; 4 and 5 are old but not yet upstream.
    (state / "forward").mkdir(parents=True)
    (state / "forward" / "cursor.json").write_text(json.dumps({"after": 3}))

    make_signals_time_partitioned(conn, TimeseriesPolicy(chunk_days=1))
    up = pg_upsert(conn.cursor())
    today = NOW.replace(hour=0, minute=0, second=0, microsecond=0)
    for d in [*OLD_DAYS, 0]:
        for k in range(300):
            up(_silver_row(today - timedelta(days=d) + timedelta(hours=6), k))
    # Old chunks compressed now, as the archive's policy would have long ago,
    # so the background job has nothing left to change sizes mid-test.
    conn.execute("SELECT compress_chunk(c, if_not_compressed => true) "
                 "FROM show_chunks('silver.signals', older_than => interval '3 days') c")
    return SimpleNamespace(state=state, bronze=bronze, outbox=outbox, outbox_dir=outbox_dir,
                           dsn=timescale_dsn, conn=conn)


def _chunks(conn) -> list[tuple[str, int]]:
    return [(r[0], int(r[1])) for r in conn.execute(
        "SELECT c.chunk_name, s.total_bytes FROM timescaledb_information.chunks c "
        "JOIN chunks_detailed_size('silver.signals') s ON s.chunk_name = c.chunk_name "
        "WHERE c.hypertable_schema = 'silver' AND c.hypertable_name = 'signals' "
        "ORDER BY c.range_start")]


def _batch_files(bronze: Path, outbox: EdgeOutbox, seq: int) -> list[Path]:
    rec = outbox.read(after=seq - 1, limit=1)[0]
    h = rec["content_hash"]
    return [bronze / SOURCE / "_content" / h[:2] / h,
            *(bronze / SOURCE / "_rows").glob(f"*/{rec['item_id']}.jsonl")]


def _used(a) -> int:
    return bronze_bytes([a.bronze]) + silver_bytes(a.conn)


def _reclaim(a, *, budget: int, capsys) -> tuple[int, str]:
    code = cli.main(["archive-reclaim", "--outbox-dir", str(a.outbox_dir), "--keep-days", "30",
                     "--budget-bytes", str(budget), "--dsn", a.dsn])
    return code, capsys.readouterr().out


def _status_row(state: Path) -> dict:
    from axiom.extensions.builtins.status import function_status

    sections = function_status.sections(SimpleNamespace(confined=True, settings={}),
                                        outbox_dir=state.parent / "outbox")
    archive = next(s for s in sections if s["title"] == "Local archive")
    return next(r for r in archive["rows"] if r["label"] == "retention")


def test_old_delivered_data_rotates_out_oldest_first_and_the_alarm_clears(archive, capsys):
    a = archive
    delivered = [f for seq in (1, 2, 3) for f in _batch_files(a.bronze, a.outbox, seq)]
    undelivered = [f for seq in (4, 5, 6) for f in _batch_files(a.bronze, a.outbox, seq)]
    assert all(f.exists() for f in delivered + undelivered)
    chunks = _chunks(a.conn)
    assert len(chunks) == 5
    freed_bronze = sum(f.stat().st_size for f in delivered)
    # Over budget by exactly the delivered bronze plus the oldest chunk, plus a
    # byte: clearing it takes the bronze and the TWO oldest chunks, no more.
    budget = _used(a) - freed_bronze - chunks[0][1] - 1

    code, out = _reclaim(a, budget=budget, capsys=capsys)

    assert code == 0, out
    assert "alarm cleared" in out
    assert not any(f.exists() for f in delivered)
    assert all(f.exists() for f in undelivered)  # 4, 5 old but not upstream; 6 young
    left = [name for name, _ in _chunks(a.conn)]
    assert left == [name for name, _ in chunks[2:]]  # oldest two gone, the rest kept
    assert _used(a) <= budget
    # The outbox is never rewritten: sequence numbers carry on.
    assert a.outbox.last_seq() == 6
    row = _status_row(a.state)
    assert row["state"] == "ok" and "pressure cleared" in row["value"]
    assert "3 not yet delivered upstream are kept" in row["value"]

    # A second pass under the same budget finds no pressure and deletes nothing.
    code, out = _reclaim(a, budget=budget, capsys=capsys)
    assert code == 0 and "no pressure" in out
    assert [n for n, _ in _chunks(a.conn)] == left


def test_undelivered_and_young_data_is_kept_even_when_the_alarm_cannot_clear(archive, capsys):
    a = archive
    undelivered = [f for seq in (4, 5, 6) for f in _batch_files(a.bronze, a.outbox, seq)]
    today_chunk = _chunks(a.conn)[-1][0]

    code, out = _reclaim(a, budget=1, capsys=capsys)

    assert code != 0, out  # still under pressure: says so and exits non-zero
    assert "ALARM STILL UP" in out and "await delivery upstream" in out
    assert all(f.exists() for f in undelivered)
    assert [n for n, _ in _chunks(a.conn)] == [today_chunk]  # every old chunk, never today's
    from ...forward.forwarder import LocalOutbox

    local = LocalOutbox(a.outbox_dir, bronze_root_for=lambda _s: a.bronze)
    for rec in a.outbox.read(after=3, limit=10):  # still deliverable, byte for byte
        assert hashlib.sha256(local.content(SOURCE, rec["content_hash"])).hexdigest() == \
            rec["content_hash"]
    assert _status_row(a.state)["state"] == "fail"


def test_with_no_retention_declared_nothing_is_deleted(archive, tmp_path):
    from ...archive_space import reclaim

    a = archive
    before = _chunks(a.conn)
    report = reclaim(outbox_dir=a.outbox_dir, bronze_root_for=lambda _s: a.bronze,
                     bronze_roots=[a.bronze], policy=ArchivePolicy(keep_days=None, budget_bytes=1),
                     forwarded_through=3, dsn=a.dsn, disk_path=tmp_path, now=time.time())
    assert report["alarm"] and report["deleted_batches"] == 0 and not report["dropped_chunks"]
    assert _chunks(a.conn) == before
    assert any("nothing is deleted" in n for n in report["notes"])
