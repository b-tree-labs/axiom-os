# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Silver's identity is the reading (ADR-206), and a rebuild replaces in place.

The requirement, as the owner put it: somebody who wants to reprocess data
that is already in gold with new conforming logic, because something is wrong
in gold, must be able to. Against the transport key that was impossible
without duplicating: the same reading arriving through a second file, or a
second connector, had a second ``row_hash`` and landed twice.

Every test here starts from a known schema (silver and gold dropped and
recreated) in a throwaway TimescaleDB container, never a developer database.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import psycopg
import pytest

from axiom.extensions.builtins.data_platform.conformance import (
    GOLD_SIGNALS_DDL,
    SILVER_DDL,
    NormalizerRegistry,
)
from axiom.extensions.builtins.data_platform.conformance.runner import run_conform
from axiom.extensions.builtins.data_platform.conformance.watermarks import WATERMARKS_TABLE

SCHEMA_REF = "probe/frame-v1"
SITE = "site-a"
FEED = "probe.live"
DAY1, DAY2 = "2026-10-01", "2026-10-02"


def _normalizer(scale: float = 1.0, offset: float = 0.0):
    """A normalizer whose LOGIC can be changed: the unit conversion."""

    def _normalize(rec):
        row = rec["row"]
        yield {
            "feed": FEED, "channel": row["ch"], "ts": row["ts"],
            "value": float(row["v"]) * scale + offset, "unit": "degC",
            "quality": "good", "source_class": "measured",
        }

    return _normalize


def _ts(day: str, i: int) -> str:
    return (datetime.fromisoformat(f"{day}T00:00:00+00:00") + timedelta(seconds=i)).isoformat()


def _line(day: str, i: int, *, ch: str = "T1", salt: str = "") -> str:
    """One bronze record. ``salt`` changes the transport hash and nothing else,
    which is exactly what a second file or a second connector does."""
    return json.dumps({
        "item_id": f"{day}-{ch}-{i}{salt}",
        "schema_ref": SCHEMA_REF,
        "row_hash": hashlib.sha256(f"{salt}|{day}|{ch}|{i}".encode()).hexdigest(),
        "row": {"ts": _ts(day, i), "ch": ch, "v": float(i)},
    })


def _write(root: Path, connector: str, name: str, lines: list[str], *, day: str) -> None:
    path = root / connector / "_rows" / day / f"{name}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(line + "\n" for line in lines))


@pytest.fixture
def env(timescale_dsn, tmp_path):
    from axiom.extensions.builtins.data_platform.agents.plinth.connectors import (
        ConnectorConfig,
        save_connector,
    )

    with psycopg.connect(timescale_dsn, autocommit=True) as c:
        c.execute("DROP SCHEMA IF EXISTS gold CASCADE")
        c.execute("DROP SCHEMA IF EXISTS silver CASCADE")
        c.execute(f"DROP TABLE IF EXISTS {WATERMARKS_TABLE}")
        for stmt in SILVER_DDL + GOLD_SIGNALS_DDL:
            c.execute(stmt)
    state = tmp_path / "state"
    state.mkdir()
    root = tmp_path / "bronze"
    root.mkdir()
    for name in ("primary", "backfill"):
        save_connector(
            ConnectorConfig(name=name, kind="push", site=SITE, bronze_root=str(root),
                            params={"schema_ref": SCHEMA_REF}),
            state_dir=state,
        )
    holder = {"fn": _normalizer()}

    def run(**over):
        registry = NormalizerRegistry()
        registry.register(SCHEMA_REF, holder["fn"])
        kw = dict(bronze_root=root, dsn=timescale_dsn, state_dir=state, registry=registry, batch_size=50)
        kw.update(over)
        return run_conform(**kw)

    def q(sql, *args):
        with psycopg.connect(timescale_dsn) as c:
            return c.execute(sql, args).fetchall()

    return SimpleNamespace(root=root, run=run, q=q, holder=holder, dsn=timescale_dsn)


def _rows(env, day: str | None = None) -> int:
    if day is None:
        return env.q("SELECT count(*) FROM silver.signals")[0][0]
    return env.q(
        "SELECT count(*) FROM silver.signals WHERE ts >= %s::date AND ts < %s::date + 1", day, day
    )[0][0]


def test_the_same_readings_through_two_connectors_and_two_files_are_one_row(env):
    _write(env.root, "primary", "a", [_line(DAY1, i) for i in range(40)], day=DAY1)
    # The same readings again: a second file of the same connector...
    _write(env.root, "primary", "a-redelivered", [_line(DAY1, i, salt="r") for i in range(40)], day=DAY1)
    # ...and a second connector (a backfill) normalizing to the same feed.
    _write(env.root, "backfill", "b", [_line(DAY1, i, salt="b") for i in range(40)], day=DAY1)
    stats = env.run()
    assert _rows(env) == 40, "one reading, one row, however many paths carried it"
    assert stats["writes"] == {"inserted": 40, "replaced": 0, "unchanged": 80, "reidentified": 0}
    assert env.q("SELECT count(*) FROM gold.signals")[0][0] == 40
    assert env.q("SELECT rows FROM gold.signal_catalogue WHERE channel = 'T1'")[0][0] == 40


def test_a_rebuild_with_corrected_logic_replaces_in_place_and_refreshes_gold(env):
    _write(env.root, "primary", "a", [_line(DAY1, i) for i in range(60)], day=DAY1)
    env.run()
    before_revision = env.q("SELECT computed_at FROM gold.signal_catalogue")[0][0]
    assert env.q("SELECT value FROM gold.signals WHERE ts = %s", _ts(DAY1, 10))[0][0] == 10.0

    # The fix: the unit conversion was wrong.
    env.holder["fn"] = _normalizer(scale=1.8, offset=32.0)
    out = env.run(rebuild=True, connectors=frozenset({"primary"}), start=DAY1, end=DAY2)

    assert out["writes"]["replaced"] == 60 and out["writes"]["inserted"] == 0
    assert _rows(env) == 60, "replaced, not added"
    assert env.q("SELECT value FROM silver.signals WHERE ts = %s", _ts(DAY1, 10))[0][0] == pytest.approx(50.0)
    assert env.q("SELECT value FROM gold.signals WHERE ts = %s", _ts(DAY1, 10))[0][0] == pytest.approx(50.0)
    latest = env.q("SELECT value FROM gold.signals_latest WHERE channel = 'T1'")[0][0]
    assert latest == pytest.approx(59 * 1.8 + 32)
    assert out["gold_refreshed"] == [SITE]
    rows, revision = env.q("SELECT rows, computed_at FROM gold.signal_catalogue")[0]
    assert rows == 60 and revision > before_revision, "exact count, new revision"
    stamps = env.q("SELECT count(*) FROM silver.signals WHERE content_hash IS NOT NULL "
                   "AND conform_version IS NOT NULL AND conformed_at IS NOT NULL")[0][0]
    assert stamps == 60

    # The identical second rebuild writes nothing.
    tuples = env.q("SELECT xmin::text, conformed_at FROM silver.signals ORDER BY ts")
    again = env.run(rebuild=True, connectors=frozenset({"primary"}), start=DAY1, end=DAY2)
    assert again["writes"] == {"inserted": 0, "replaced": 0, "unchanged": 60, "reidentified": 0}
    assert env.q("SELECT xmin::text, conformed_at FROM silver.signals ORDER BY ts") == tuples


def test_a_rebuild_over_a_window_touches_only_that_window(env):
    _write(env.root, "primary", "d1", [_line(DAY1, i) for i in range(30)], day=DAY1)
    _write(env.root, "primary", "d2", [_line(DAY2, i) for i in range(30)], day=DAY2)
    env.run()
    day2_before = env.q("SELECT xmin::text, value FROM silver.signals WHERE ts >= %s::date ORDER BY ts", DAY2)

    env.holder["fn"] = _normalizer(offset=100.0)
    out = env.run(rebuild=True, start=DAY1, end=DAY2)
    assert out["writes"]["replaced"] == 30 and out["out_of_scope"] == 30
    assert _rows(env) == 60
    assert env.q("SELECT min(value) FROM silver.signals WHERE ts < %s::date", DAY2)[0][0] == 100.0
    assert env.q(
        "SELECT xmin::text, value FROM silver.signals WHERE ts >= %s::date ORDER BY ts", DAY2
    ) == day2_before, "a reading outside the window is not written"


def test_a_dry_run_rebuild_reports_exact_counts_and_writes_nothing(env):
    _write(env.root, "primary", "a", [_line(DAY1, i) for i in range(20)], day=DAY1)
    env.run()
    env.holder["fn"] = _normalizer(offset=1.0)
    out = env.run(rebuild=True, apply=False)
    assert out["writes"]["replaced"] == 20 and out["applied"] is False
    assert env.q("SELECT max(value) FROM silver.signals")[0][0] == 19.0


def test_a_rebuild_refuses_without_the_identity_key(env):
    """A populated node before its migration keeps the legacy key, and a rebuild
    there would duplicate a reading re-arriving through another path."""
    from axiom.extensions.builtins.data_platform.conformance.runner import RebuildNeedsIdentity

    _write(env.root, "primary", "a", [_line(DAY1, i) for i in range(5)], day=DAY1)
    assert env.run()["identity_key"] is True
    # A populated table: the DDL never rebuilds the index on one.
    with psycopg.connect(env.dsn, autocommit=True) as c:
        c.execute("DROP INDEX silver.signals_reading_identity")
    _write(env.root, "primary", "b", [_line(DAY1, i) for i in range(5, 10)], day=DAY1)
    later = env.run()
    assert later["identity_key"] is False and _rows(env) == 10, "the legacy writer still works"
    with pytest.raises(RebuildNeedsIdentity):
        env.run(rebuild=True)


def test_the_preflight_finds_planted_duplicates_and_collapse_keeps_the_newest(env):
    from axiom.extensions.builtins.data_platform.conformance import reading_identity as ri

    # Legacy-key history: the same readings twice (a re-send) on T1, and two
    # disagreeing rows at one instant (divergent) on T2.
    _write(env.root, "primary", "a", [_line(DAY1, i) for i in range(10)], day=DAY1)
    env.run()
    with psycopg.connect(env.dsn, autocommit=True) as c:
        c.execute("DROP INDEX silver.signals_reading_identity")
    _write(env.root, "backfill", "b", [_line(DAY1, i, salt="b") for i in range(10)], day=DAY1)
    env.run()
    with psycopg.connect(env.dsn, autocommit=True) as c:
        for v, h in ((1.0, "div-old"), (2.0, "div-new")):
            c.execute(
                "INSERT INTO silver.signals (site, feed, channel, ts, value, schema_ref, row_hash, "
                "conformed_at) VALUES (%s, %s, 'T2', %s, %s, 'x', %s, %s)",
                (SITE, FEED, _ts(DAY1, 0), v, h,
                 datetime(2026, 10, 3, tzinfo=UTC) if h == "div-new" else datetime(2026, 10, 2, tzinfo=UTC)),
            )
    assert _rows(env) == 22

    with psycopg.connect(env.dsn) as c, c.cursor() as cur:
        report = ri.preflight(cur, site=SITE)
    assert report["index_present"] is False
    assert report["totals"] == {"identities": 11, "extra_rows": 11, "resend": 10, "divergent": 1}
    assert _rows(env) == 22, "preflight is read-only"

    with psycopg.connect(env.dsn) as c:
        dry = ri.collapse(c, site=SITE)
    assert dry["removed"] == 10 and dry["divergent_left"] == 1 and _rows(env) == 22

    with psycopg.connect(env.dsn) as c:
        done = ri.collapse(c, site=SITE, apply=True, accept_divergent=True)
    assert done["removed"] == 11 and _rows(env) == 11
    assert env.q("SELECT value FROM silver.signals WHERE channel = 'T2'") == [(2.0,)], "newest conform kept"
    audit = env.q(f"SELECT collapse_reason, count(*) FROM {ri.AUDIT_TABLE} GROUP BY 1 ORDER BY 1")
    assert audit == [("divergent", 1), ("resend", 10)], "every removed row is kept in the audit table"

    with psycopg.connect(env.dsn, autocommit=True) as c:
        built = ri.build_index(c)
    assert built["built"] is True
    with psycopg.connect(env.dsn) as c, c.cursor() as cur:
        assert ri.has_identity_key(cur)


def test_compressed_chunks_take_the_upsert_or_are_named_by_the_build_path(env):
    """A hypertable whose identity index predates compression (a fresh install)
    upserts into a compressed chunk; one that must build the index afterwards
    is told exactly which path its TimescaleDB version allows."""
    from axiom.extensions.builtins.data_platform.conformance import reading_identity as ri
    from axiom.extensions.builtins.data_platform.conformance.timeseries import (
        TimeseriesPolicy,
        make_signals_time_partitioned,
    )

    with psycopg.connect(env.dsn, autocommit=True) as c:
        make_signals_time_partitioned(c, TimeseriesPolicy(chunk_days=1, compress_after_days=1))
    _write(env.root, "primary", "a", [_line(DAY1, i) for i in range(30)], day=DAY1)
    env.run()
    with psycopg.connect(env.dsn, autocommit=True) as c:
        c.execute("SELECT compress_chunk(c) FROM show_chunks('silver.signals') c")
        assert c.execute(
            "SELECT bool_and(is_compressed) FROM timescaledb_information.chunks "
            "WHERE hypertable_name = 'signals'"
        ).fetchone()[0]
        with c.cursor() as cur:
            assert ri.has_identity_key(cur)
            path = ri.build_path(ri._layout(cur))
    env.holder["fn"] = _normalizer(offset=5.0)
    out = env.run(rebuild=True, start=DAY1, end=DAY2)
    assert out["writes"]["replaced"] == 30 and _rows(env) == 30
    assert env.q("SELECT min(value) FROM gold.signals")[0][0] == 5.0
    # The build path a node that has to ADD the index now would be given:
    # measured, 2.17.2 refuses a unique index once compression is enabled.
    with psycopg.connect(env.dsn) as c, c.cursor() as cur:
        layout = ri._layout(cur)
    version = tuple(int(p) for p in layout["timescaledb"].split("."))
    expected = (
        "hypertable-blocking-writes"
        if version >= ri.TIMESCALE_VERIFIED_UNIQUE_ON_COMPRESSED
        else "upgrade-timescaledb-first"
    )
    assert layout["compression_enabled"] and path["method"] == expected


def test_the_conform_run_verb_prints_inserted_replaced_and_unchanged():
    from axiom.extensions.builtins.data_platform.skills.conform_run import _report

    stats = {
        "rows_in": 60, "rows_out": 60, "identity_key": True, "rebuilt": True, "applied": False,
        "writes": {"inserted": 0, "replaced": 60, "unchanged": 0, "reidentified": 0},
        "scope": {"connectors": ["primary"], "sites": None, "start": "2026-10-01T00:00:00+00:00",
                  "end": "2026-10-02T00:00:00+00:00"},
        "gold_refreshed": [SITE], "distributions_loaded": ["<injected>"],
    }
    result = _report(stats, strict=False, state_dir=None)
    text = "\n".join(result.actions_taken)
    assert "rebuild: inserted 0, replaced 60, unchanged 0 (dry run: nothing written)" in text
    assert "rebuild scope: connector primary, every site, from 2026-10-01" in text
    assert result.value["replaced"] == 60


def test_a_rebuild_that_moves_a_reading_to_another_identity_moves_it(env):
    """Corrected logic that renames the feed: the same bronze reading now has a
    different identity. It must move, not collide on the transport key and not
    leave a stale copy under the old feed."""
    _write(env.root, "primary", "a", [_line(DAY1, i) for i in range(10)], day=DAY1)
    env.run()

    def renamed(rec):
        for row in _normalizer()(rec):
            yield {**row, "feed": "probe.corrected"}

    env.holder["fn"] = renamed
    out = env.run(rebuild=True)
    assert out["writes"]["reidentified"] == 10 and out["writes"]["inserted"] == 10
    assert env.q("SELECT feed, count(*) FROM silver.signals GROUP BY 1") == [("probe.corrected", 10)]
