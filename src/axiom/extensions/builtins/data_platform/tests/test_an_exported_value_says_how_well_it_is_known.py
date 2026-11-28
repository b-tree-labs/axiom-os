# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""An exported value carries its uncertainty in one of three kinds, never by silence (ADR-136)."""

from __future__ import annotations

import json
import logging
import uuid

import pytest

from axiom.extensions.builtins.data_platform import export_file as ef
from axiom.extensions.builtins.data_platform.skills import export as ex
from axiom.infra.principal import PrincipalContext
from axiom.infra.skills import SkillContext, SkillRegistry, SkillResult

psycopg2 = pytest.importorskip("psycopg2")

TABLE = f"export_unc_{uuid.uuid4().hex[:8]}"


def _ctx(tmp_path):
    return SkillContext(registry=SkillRegistry(), state_dir=tmp_path, logger=logging.getLogger("t"),
                        principal=PrincipalContext(handle="@tester", posture="attested", assured=True),
                        surface="cli")


def _said(path):
    return json.loads(ef.sidecar_path(path).read_text())["uncertainty"]


def _door(rows, columns):
    def table(params, ctx):
        if params["dataset"].endswith("_uncertainty"):
            return SkillResult(ok=False, errors=["no such relation"])
        return SkillResult(ok=True, value={"rows": rows, "next": None,
                                           "spec": {"columns": [{"id": c} for c in columns]}})
    return table


def test_a_table_with_no_uncertainty_column_says_none_was_reported(tmp_path):
    rows = [{"ts": "2026-10-01T00:00:00Z", "channel": "a", "value": 1.0}]
    out = tmp_path / "n.csv"
    done = ex.export({"out": str(out)}, _ctx(tmp_path), table=_door(rows, ["ts", "channel", "value"]))
    assert done.ok and _said(out)["kind"] == ef.NONE_REPORTED
    assert "not zero" in _said(out)["note"] and "none reported" in done.value["text"]


def test_a_magnitude_is_counted_and_a_zero_or_missing_one_is_not_a_claim(tmp_path):
    rows = [{"ts": f"2026-10-01T00:00:0{i}Z", "channel": "a", "value": 1.0 + i, "uncertainty": u}
            for i, u in enumerate([0.2, 0.0, None, 0.2])]
    out = tmp_path / "m.csv"
    done = ex.export({"out": str(out)}, _ctx(tmp_path),
                     table=_door(rows, ["ts", "channel", "value", "uncertainty"]))
    assert done.ok
    said = _said(out)
    assert said["kind"] == ef.MAGNITUDE_ONLY
    assert said["columns"]["value"] == {"from": "uncertainty", "stated": 2, "not_stated": 2}
    assert "too confident" in said["note"]
    # The unstated ones are empty in the file, never 0.
    assert [r["uncertainty"] for r in ef.read_rows(out)] == ["0.2", "0.0", None, "0.2"]


def test_the_uncertainty_column_is_written_even_when_it_was_not_asked_for(tmp_path):
    rows = [{"ts": "2026-10-01T00:00:00Z", "channel": "a", "value": 1.0, "uncertainty": 0.2}]
    out = tmp_path / "c.csv"
    done = ex.export({"out": str(out), "columns": ["ts", "value"]}, _ctx(tmp_path),
                     table=_door(rows, ["ts", "channel", "value", "uncertainty"]))
    assert done.ok and done.value["columns"] == ["ts", "value", "uncertainty"]


@pytest.fixture(scope="module")
def dsn(_scratch_postgres_url):
    from axiom.infra.db import libpq_url

    url = libpq_url(_scratch_postgres_url)
    conn = psycopg2.connect(url)
    conn.autocommit = True
    c = conn.cursor()
    c.execute("CREATE SCHEMA IF NOT EXISTS gold")
    c.execute(f"CREATE TABLE gold.{TABLE} (id int PRIMARY KEY, site text NOT NULL, ts timestamptz NOT NULL, "
              f"channel text NOT NULL, value float8, uncertainty float8, unit text)")
    c.execute(f"INSERT INTO gold.{TABLE} SELECT i, 'site-alpha', timestamptz '2026-10-01' + make_interval(secs => i), "
              f"'tc-14', 20 + i * 0.1, 0.5, 'degC' FROM generate_series(1, 40) i")
    # The companion: every reading shares the instrument's calibration, and has its own repeatability.
    c.execute(f"CREATE TABLE gold.{TABLE}_uncertainty (site text NOT NULL, ts timestamptz NOT NULL, "
              f"channel text NOT NULL, symbol text NOT NULL, coefficient float8 NOT NULL, independent boolean NOT NULL, "
              f"PRIMARY KEY (site, ts, channel, symbol))")
    c.execute(f"INSERT INTO gold.{TABLE}_uncertainty SELECT site, ts, channel, 'signals:tc-14:calibration', 0.4, false "
              f"FROM gold.{TABLE}")
    c.execute(f"INSERT INTO gold.{TABLE}_uncertainty SELECT site, ts, channel, 'signals:tc-14:repeatability', 0.3, true "
              f"FROM gold.{TABLE}")
    for t in (TABLE, f"{TABLE}_uncertainty"):
        c.execute(f"CREATE INDEX ON gold.{t} (ts)")
        c.execute(f"ANALYZE gold.{t}")
    yield url
    c.execute(f"DROP TABLE gold.{TABLE}_uncertainty")
    c.execute(f"DROP TABLE gold.{TABLE}")
    conn.close()


def test_the_sources_are_written_beside_the_values_and_named_by_their_site(dsn, monkeypatch, tmp_path):
    monkeypatch.setenv("DATABASE_URL", dsn)
    out = tmp_path / "tc.csv"
    done = ex.export({"dataset": TABLE, "channels": ["tc-14"], "start": "2026-10-01T00:00:11Z",
                      "end": "2026-10-01T00:00:21Z", "sort": ["ts"], "out": str(out)}, _ctx(tmp_path))
    assert done.ok, done.errors
    said = _said(out)
    assert said["kind"] == ef.SOURCES_KNOWN, done.value["text"]
    assert said["sources_file"] == "tc.uncertainty-sources.csv" and said["sources_rows"] == 20
    assert said["sources_qualified_by"] == "site"
    sources = list(ef.read_rows(tmp_path / said["sources_file"]))
    # Ten readings, two sources each, and the same filter bounded both files.
    assert len(sources) == 20
    symbols = {r["symbol"] for r in sources}
    # Qualified by origin: another site's tc-14 is a different instrument.
    assert symbols == {"@site-alpha/signals:tc-14:calibration", "@site-alpha/signals:tc-14:repeatability"}
    # The calibration is ONE symbol across all ten readings: that is what says it does not average away.
    assert sum(1 for r in sources if r["symbol"].endswith("calibration")) == 10
    assert ef.verify(tmp_path / said["sources_file"])[0] and ef.verify(out)[0]
    assert "sources in tc.uncertainty-sources.csv" in done.value["text"]


def test_asking_for_no_sources_leaves_a_magnitude_and_says_so(dsn, monkeypatch, tmp_path):
    monkeypatch.setenv("DATABASE_URL", dsn)
    out = tmp_path / "plain.csv"
    done = ex.export({"dataset": TABLE, "sort": ["ts"], "out": str(out), "sources": False}, _ctx(tmp_path))
    assert done.ok and _said(out)["kind"] == ef.MAGNITUDE_ONLY
    assert not (tmp_path / "plain.uncertainty-sources.csv").exists()


VIEW = f"export_view_of_{uuid.uuid4().hex[:8]}"


@pytest.fixture(scope="module")
def served_as_views(dsn):
    """The shape a node really serves: views over keyed tables, the sources keyed by row hash."""
    from axiom.extensions.builtins.data_platform import table_door as td

    conn = psycopg2.connect(dsn)
    conn.autocommit = True
    c = conn.cursor()
    c.execute(f"CREATE TABLE gold.{VIEW}_t (row_hash text NOT NULL, channel text NOT NULL, site text NOT NULL, "
              f"feed text NOT NULL, ts timestamptz NOT NULL, value float8, uncertainty float8, unit text, "
              f"PRIMARY KEY (row_hash, channel))")
    c.execute(f"INSERT INTO gold.{VIEW}_t SELECT md5(i::text), 'tc-14', 'site-alpha', 'loop', "
              f"timestamptz '2026-10-01' + make_interval(secs => i), 20 + i * 0.1, 0.5, 'degC' "
              f"FROM generate_series(1, 1500) i")
    c.execute(f"CREATE INDEX ON gold.{VIEW}_t (site, feed, channel, ts)")
    c.execute(f"CREATE TABLE gold.{VIEW}_u (row_hash text NOT NULL, channel text NOT NULL, symbol text NOT NULL, "
              f"coefficient float8 NOT NULL, independent boolean NOT NULL, PRIMARY KEY (row_hash, channel, symbol))")
    c.execute(f"INSERT INTO gold.{VIEW}_u SELECT row_hash, channel, 'signals:tc-14:calibration', 0.4, false "
              f"FROM gold.{VIEW}_t")
    c.execute(f"CREATE VIEW gold.{VIEW} AS SELECT * FROM gold.{VIEW}_t")
    c.execute(f"CREATE VIEW gold.{VIEW}_uncertainty AS SELECT * FROM gold.{VIEW}_u")
    td.declare_table_source(td.TableSourceDeclaration(
        table=VIEW, key=("row_hash", "channel"), sortable=("ts",), cheap=("ts",)))
    td.declare_table_source(td.TableSourceDeclaration(
        table=f"{VIEW}_uncertainty", key=("row_hash", "channel", "symbol")))
    yield dsn
    for name in (VIEW, f"{VIEW}_uncertainty"):
        td._DECLARATIONS.pop(name, None)
        c.execute(f"DROP VIEW gold.{name}")
    c.execute(f"DROP TABLE gold.{VIEW}_t")
    c.execute(f"DROP TABLE gold.{VIEW}_u")
    conn.close()


def test_sources_keyed_by_the_row_are_found_for_a_window_of_time(served_as_views, monkeypatch, tmp_path):
    """The companion has no time of its own, so the window cannot be put to it: it is joined by key."""
    monkeypatch.setenv("DATABASE_URL", served_as_views)
    out = tmp_path / "w.csv"
    done = ex.export({"dataset": VIEW, "channels": ["tc-14"], "feed": "loop", "sort": ["ts"],
                      "start": "2026-10-01T00:05:00Z", "end": "2026-10-01T00:10:00Z", "out": str(out)},
                     _ctx(tmp_path))
    assert done.ok, done.errors
    assert done.value["rows"] == 300
    said = _said(out)
    assert said["kind"] == ef.SOURCES_KNOWN, done.value["text"]
    sources = list(ef.read_rows(tmp_path / said["sources_file"]))
    # Exactly the 300 readings in the window, not the 1,500 in the table.
    assert len(sources) == 300 == said["sources_rows"]
    assert {r["row_hash"] for r in sources} == {r["row_hash"] for r in ef.read_rows(out)}
    assert {r["symbol"] for r in sources} == {"@site-alpha/signals:tc-14:calibration"}


def test_the_platform_declares_how_its_signal_views_are_paged():
    from axiom.extensions.builtins.data_platform import table_door as td

    declared = {d.table: d for d in td.declared_table_sources()}
    assert declared["signals"].key == ("row_hash", "channel") and "ts" in declared["signals"].cheap
    assert declared["signals_uncertainty"].key == ("row_hash", "channel", "symbol")


def test_readings_come_in_the_order_they_were_taken_when_nobody_says():
    assert ex._request({"dataset": "signals"})["sort"] == ["ts"]
    assert "sort" not in ex._request({"dataset": "something_else"})
