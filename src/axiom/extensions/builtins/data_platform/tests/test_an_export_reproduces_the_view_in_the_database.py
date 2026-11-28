# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""An export through the real table door holds exactly the rows the view has (spec-data-table, phase 3)."""

from __future__ import annotations

import json
import logging
import uuid

import pytest

from axiom.extensions.builtins.data_platform import export_file as ef
from axiom.extensions.builtins.data_platform.skills import export as ex
from axiom.infra.principal import PrincipalContext
from axiom.infra.skills import SkillContext, SkillRegistry

psycopg2 = pytest.importorskip("psycopg2")

TABLE = f"export_view_{uuid.uuid4().hex[:8]}"
ROWS = 2600  # three windows of the door's 1,000-row ceiling


def _ctx(tmp_path, surface="cli"):
    return SkillContext(registry=SkillRegistry(), state_dir=tmp_path, logger=logging.getLogger("t"),
                        principal=PrincipalContext(handle="@tester", posture="attested", assured=True),
                        surface=surface)


@pytest.fixture(scope="module")
def dsn(_scratch_postgres_url):
    from axiom.infra.db import libpq_url

    url = libpq_url(_scratch_postgres_url)
    conn = psycopg2.connect(url)
    conn.autocommit = True
    c = conn.cursor()
    c.execute("CREATE SCHEMA IF NOT EXISTS gold")
    c.execute(f"CREATE TABLE gold.{TABLE} (id int PRIMARY KEY, ts timestamptz NOT NULL, channel text NOT NULL, "
              f"value float8, unit text, source_class text NOT NULL)")
    c.execute(f"INSERT INTO gold.{TABLE} SELECT i, timestamptz '2026-10-01' + make_interval(secs => i), "
              f"CASE WHEN i % 2 = 0 THEN 'loop.flow' ELSE 'loop.inlet_temperature' END, "
              f"CASE WHEN i = 7 THEN NULL ELSE i * 0.5 END, "
              f"CASE WHEN i % 2 = 0 THEN 'kg/s' ELSE 'degC' END, "
              f"CASE WHEN i > 2500 THEN 'predicted' ELSE 'measured' END FROM generate_series(1, {ROWS}) i")
    c.execute(f"CREATE INDEX ON gold.{TABLE} (ts)")
    c.execute(f"ANALYZE gold.{TABLE}")
    yield url
    c.execute(f"DROP TABLE gold.{TABLE}")
    conn.close()


def test_the_file_holds_every_row_of_the_view_once_and_in_order(dsn, monkeypatch, tmp_path):
    monkeypatch.setenv("DATABASE_URL", dsn)
    out = tmp_path / "view.csv"
    done = ex.export({"dataset": TABLE, "sort": ["ts"], "out": str(out)}, _ctx(tmp_path), sleep=lambda s: None)
    assert done.ok, done.errors
    assert done.value["rows"] == ROWS and done.value["complete"] is True
    rows = list(ef.read_rows(out))
    assert [int(r["id"]) for r in rows] == list(range(1, ROWS + 1))  # no row twice, none missing
    assert rows[6]["value"] is None  # the reading stored as NULL is not a zero in the file
    said = json.loads(ef.sidecar_path(out).read_text())
    assert said["source_class"] == ["measured", "predicted"]
    assert said["units"] == {"loop.flow": "kg/s", "loop.inlet_temperature": "degC"}
    assert said["first_ts"].startswith("2026-10-01T00:00:01") and said["exported_by"] == "@tester"
    assert ef.verify(out)[0]


def test_the_window_and_channels_a_person_names_bound_the_file(dsn, monkeypatch, tmp_path):
    pytest.importorskip("pyarrow")
    monkeypatch.setenv("DATABASE_URL", dsn)
    out = tmp_path / "hour.parquet"
    done = ex.export({"dataset": TABLE, "channels": ["loop.flow"], "start": "2026-10-01T00:10:00Z",
                      "end": "2026-10-01T00:20:00Z", "sort": ["ts"], "format": "parquet", "out": str(out)},
                     _ctx(tmp_path, "mcp"), sleep=lambda s: None)
    assert done.ok, done.errors
    rows = list(ef.read_rows(out))
    assert len(rows) == 300 and {r["channel"] for r in rows} == {"loop.flow"}
    assert done.value["first_ts"].startswith("2026-10-01T00:10:00")
    assert ef.read_provenance(out)["surface"] == "mcp"


def test_an_unknown_table_is_refused_and_nothing_is_written(dsn, monkeypatch, tmp_path):
    monkeypatch.setenv("DATABASE_URL", dsn)
    done = ex.export({"dataset": "no_such_table", "out": str(tmp_path / "x.csv")}, _ctx(tmp_path))
    assert not done.ok and not (tmp_path / "x.csv").exists()
