# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tests for the sql-tabular source kind (ADR-001 P2).

The DSN is resolved from ``credential_ref`` through the secrets extension
(``env://`` provider, so no real secret store needed) and the psycopg driver is
faked — no live database in CI. The point under test is the preflight ladder
(credential → reachability → sample row) and read-only row extraction.
"""

from __future__ import annotations

import sys
import types

import pytest

from axiom.extensions.builtins.data_platform.agents.plinth.connectors import ConnectorConfig
from axiom.extensions.builtins.data_platform.sources.sql_tabular import SqlTabularProvider


@pytest.fixture
def fake_psycopg(monkeypatch):
    """Inject a fake ``psycopg`` module so these tests run without the driver
    installed — CI's base env has no psycopg, and the source imports it lazily
    (it is an optional dependency of the sql-tabular kind)."""
    mod = types.ModuleType("psycopg")
    monkeypatch.setitem(sys.modules, "psycopg", mod)
    return mod


def _cfg(*, query="SELECT d, v FROM series", credential_ref="env://P2_TEST_DSN"):
    return ConnectorConfig(
        name="preds", kind="sql-tabular", bronze_root="/tmp/b",
        credential_ref=credential_ref,
        params={"query": query, "schema_ref": "s.v1"},
    )


# ---- fake psycopg ---------------------------------------------------------


class _Col:
    def __init__(self, name):
        self.name = name


class _FakeCursor:
    def __init__(self, rows, cols):
        self._rows, self.description = rows, [_Col(c) for c in cols]

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql):
        self.sql = sql

    def fetchall(self):
        return self._rows

    def fetchone(self):
        return self._rows[0] if self._rows else None


class _FakeConn:
    def __init__(self, rows, cols):
        self._rows, self._cols = rows, cols
        self.read_only = False
        self.closed = False

    def cursor(self):
        return _FakeCursor(self._rows, self._cols)

    def close(self):
        self.closed = True


# ---- credential resolution ------------------------------------------------


def test_construct_resolves_dsn_from_credential_ref(monkeypatch):
    monkeypatch.setenv("P2_TEST_DSN", "postgresql://ro@db:5432/x?sslmode=prefer")
    source = SqlTabularProvider().construct(_cfg())
    assert source._dsn == "postgresql://ro@db:5432/x?sslmode=prefer"
    assert source.query == "SELECT d, v FROM series"


# ---- read-only extraction -------------------------------------------------


def test_run_query_maps_columns_and_opens_read_only(monkeypatch, fake_psycopg):
    monkeypatch.setenv("P2_TEST_DSN", "postgresql://ro@db/x")
    captured = {}

    def _connect(dsn, **kw):
        captured["dsn"] = dsn
        return _FakeConn(rows=[("2026-04-01", 1.0), ("2026-04-02", 2.0)], cols=["d", "v"])

    fake_psycopg.connect = _connect
    source = SqlTabularProvider().construct(_cfg())
    batch = source.fetch_rows("current")
    assert batch.rows == [{"d": "2026-04-01", "v": 1.0}, {"d": "2026-04-02", "v": 2.0}]
    assert batch.schema_ref == "s.v1"
    assert captured["dsn"] == "postgresql://ro@db/x"


# ---- preflight ladder -----------------------------------------------------


def test_preflight_without_credential_is_admin_blocker():
    res = SqlTabularProvider().preflight(_cfg(credential_ref=None))
    assert not res.ok
    b = res.blockers[0]
    assert b.name == "Credential" and b.actor == "admin"
    assert "--credential-ref" in b.remediation


def test_preflight_unreachable_is_admin_actionable(monkeypatch, fake_psycopg):
    monkeypatch.setenv("P2_TEST_DSN", "postgresql://ro@db/x")

    def _boom(dsn, **kw):
        raise OSError("could not connect to server: Connection refused")
    fake_psycopg.connect = _boom
    res = SqlTabularProvider().preflight(_cfg())
    assert not res.ok
    b = [c for c in res.blockers if c.name == "Reachability"][0]
    assert b.actor == "admin"
    assert "reachable FROM THIS HOST" in b.remediation


def test_preflight_ok_with_sample_row(monkeypatch, fake_psycopg):
    monkeypatch.setenv("P2_TEST_DSN", "postgresql://ro@db/x")
    fake_psycopg.connect = lambda dsn, **kw: _FakeConn(rows=[("2026-04-01", 1.0)], cols=["d", "v"])
    res = SqlTabularProvider().preflight(_cfg())
    assert res.ok
    assert {c.name for c in res.checks} == {"Credential", "Reachability", "Sample row"}


# ---- validation -----------------------------------------------------------


@pytest.mark.parametrize("cfg,needle", [
    (ConnectorConfig(name="p", kind="sql-tabular", bronze_root="/b",
                     params={"schema_ref": "s"}, credential_ref="env://X"), "--query"),
    (ConnectorConfig(name="p", kind="sql-tabular", bronze_root="/b",
                     params={"query": "select 1", "schema_ref": "s"}), "--credential-ref"),
    (ConnectorConfig(name="p", kind="sql-tabular", bronze_root="/b",
                     params={"query": "DELETE FROM t", "schema_ref": "s"},
                     credential_ref="env://X"), "read-only SELECT/WITH"),
])
def test_validate_flags_bad_config(cfg, needle):
    errs = SqlTabularProvider().validate(cfg)
    assert any(needle in e for e in errs), errs
