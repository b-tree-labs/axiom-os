# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""A node's status says what its local archive keeps: retention, compression, oldest day.

Read from the real archive: the declared policy, the hypertable's compression
policy and how many of its chunks are compressed, and the oldest reading it
holds (silver) beside the oldest batch waiting in bronze. A question the node
cannot answer (no database reachable) is said, never shown as fine.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from axiom.extensions.builtins.data_platform.conformance.timeseries import (
    TimeseriesPolicy,
    make_signals_time_partitioned,
)
from axiom.extensions.builtins.data_platform.ingest_sink.edge import EdgeOutbox
from axiom.extensions.builtins.status import function_status
from axiom.infra import node_functions as nf


@pytest.fixture
def archive_node(tmp_path, monkeypatch):
    cfg_file = tmp_path / "node.toml"
    monkeypatch.setenv(nf.CONFIG_ENV, str(cfg_file))
    monkeypatch.setenv("AXI_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.delenv("AXIOM_ARCHIVE_KEEP_DAYS", raising=False)
    monkeypatch.delenv("AXIOM_ARCHIVE_BUDGET_BYTES", raising=False)
    nf.apply(role="archive", functions=["acquire", "transmit"], path=cfg_file)
    return nf.load(cfg_file)


def _rows(cfg, outbox_dir=None):
    for sec in function_status.sections(cfg, outbox_dir=outbox_dir) or ():
        if sec["title"] == "Local archive":
            return {r["label"]: r for r in sec["rows"]}
    raise AssertionError("no Local archive section")


def _fill(conn, days_ago: tuple[int, ...]) -> None:
    now = datetime.now(UTC)
    for d in days_ago:
        ts = now - timedelta(days=d)
        conn.execute(
            "INSERT INTO silver.signals (site, feed, channel, ts, value, schema_ref, row_hash) "
            "VALUES ('site-a', 'daq', 'TC1', %s, 1.0, 's/v1', %s)",
            (ts, f"rh-{d}"),
        )


def test_the_archive_reports_its_policy_compression_and_oldest_day(
    archive_node, conn, timescale_dsn, tmp_path, monkeypatch
):
    monkeypatch.setenv("AXIOM_DB_URL", timescale_dsn)
    monkeypatch.setenv("AXIOM_ARCHIVE_KEEP_DAYS", "30")
    _fill(conn, (40, 10, 0))
    make_signals_time_partitioned(conn, TimeseriesPolicy(chunk_days=1, compress_after_days=2))
    conn.execute(
        "SELECT compress_chunk(c) FROM show_chunks('silver.signals', older_than => interval '2 days') c"
    )
    box = EdgeOutbox(tmp_path / "outbox")
    batch = SimpleNamespace(
        item_id="run-1", schema_ref="s/v1", etag="", source_path="", metadata={}, rows=[{}]
    )
    box.record(source="site-a-src", batch=batch, content_hash="0" * 64, rows_landed=1)

    rows = _rows(archive_node, outbox_dir=tmp_path / "outbox")
    assert rows["retention"]["state"] == "ok" and "30 days" in rows["retention"]["value"]
    assert rows["compression"]["state"] == "ok"
    assert (
        "after 2 days" in rows["compression"]["value"]
        and "2 of 3 chunks" in rows["compression"]["value"]
    )
    oldest = (datetime.now(UTC) - timedelta(days=40)).strftime("%Y-%m-%d")
    assert rows["oldest day"]["state"] == "ok" and oldest in rows["oldest day"]["value"]
    assert "bronze" in rows["oldest day"]["value"]


def test_no_retention_declared_is_said_not_assumed(archive_node, conn, timescale_dsn, monkeypatch):
    monkeypatch.setenv("AXIOM_DB_URL", timescale_dsn)
    _fill(conn, (1,))
    rows = _rows(archive_node)
    assert rows["retention"]["state"] == "warn" and "none declared" in rows["retention"]["value"]
    assert "AXIOM_ARCHIVE_KEEP_DAYS" in rows["retention"]["fix"]
    assert (
        rows["compression"]["state"] == "info"
        and "not time-partitioned" in rows["compression"]["value"]
    )


def test_an_unreachable_archive_database_is_reported_not_skipped(archive_node, monkeypatch):
    import socket

    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    monkeypatch.setenv("AXIOM_DB_URL", f"postgresql://x:y@127.0.0.1:{port}/none")
    rows = _rows(archive_node)
    assert (
        rows["compression"]["state"] == "warn"
        and "could not be read" in rows["compression"]["value"]
    )
    assert rows["oldest day"]["state"] == "warn"
