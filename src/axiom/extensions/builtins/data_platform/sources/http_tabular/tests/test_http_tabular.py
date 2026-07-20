# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tests for the http-tabular source kind (ADR-001 P2). Pure parse + preflight;
the network is monkeypatched, so no HTTP is made in CI."""

from __future__ import annotations

from axiom.extensions.builtins.data_platform.agents.plinth.connectors import ConnectorConfig
from axiom.extensions.builtins.data_platform.sources.http_tabular import (
    HttpTabularProvider,
    parse_rows,
)
from axiom.extensions.builtins.data_platform.sources.http_tabular import source as src_mod


def _cfg(**params):
    return ConnectorConfig(
        name="feed", kind="http-tabular", bronze_root="/tmp/b",
        params={"url": "https://x/data.csv", "format": "csv", "schema_ref": "s.v1", **params},
    )


# ---- parsing --------------------------------------------------------------


def test_parse_csv_with_bom_and_blank_lines():
    rows = parse_rows(b"\xef\xbb\xbfd,v\n2026-04-01,1.0\n\n2026-04-02,2.0\n", "csv")
    assert [r["d"] for r in rows] == ["2026-04-01", "2026-04-02"]


def test_parse_json_array():
    rows = parse_rows(b'[{"d":"2026-04-01","v":1},{"d":"2026-04-02","v":2}]', "json")
    assert len(rows) == 2 and rows[0]["v"] == 1


def test_parse_json_envelope():
    rows = parse_rows(b'{"rows":[{"d":"2026-04-01"}]}', "json")
    assert rows == [{"d": "2026-04-01"}]


def test_parse_json_single_object_wraps():
    rows = parse_rows(b'{"d":"2026-04-01","v":9}', "json")
    assert rows == [{"d": "2026-04-01", "v": 9}]


# ---- fetch_rows -----------------------------------------------------------


def test_fetch_rows_builds_row_batch(monkeypatch):
    monkeypatch.setattr(src_mod, "_http_get",
                        lambda url, **kw: (b"d,v\n2026-04-01,1.0\n", {"ETag": "abc"}))
    source = HttpTabularProvider().construct(_cfg())
    batch = source.fetch_rows("current")
    assert batch.source_name == "feed" and batch.etag == "abc"
    assert batch.schema_ref == "s.v1"
    assert batch.rows == [{"d": "2026-04-01", "v": "1.0"}]
    assert batch.raw == b"d,v\n2026-04-01,1.0\n"


# ---- preflight ------------------------------------------------------------


def test_preflight_ok_when_endpoint_returns_rows(monkeypatch):
    monkeypatch.setattr(src_mod, "_http_get",
                        lambda url, **kw: (b"d,v\n2026-04-01,1\n", {}))
    res = HttpTabularProvider().preflight(_cfg())
    assert res.ok
    assert {c.name for c in res.checks} == {"Reachability", "Sample rows"}


def test_preflight_unreachable_is_admin_actionable(monkeypatch):
    def _boom(url, **kw):
        raise OSError("connection refused")
    monkeypatch.setattr(src_mod, "_http_get", _boom)
    res = HttpTabularProvider().preflight(_cfg())
    assert not res.ok
    blocker = res.blockers[0]
    assert blocker.name == "Reachability" and blocker.actor == "admin"
    assert "reachable from THIS host" in blocker.remediation


def test_preflight_parse_failure_is_you_actionable(monkeypatch):
    monkeypatch.setattr(src_mod, "_http_get", lambda url, **kw: (b"not json{{", {}))
    res = HttpTabularProvider().preflight(_cfg(format="json"))
    assert not res.ok
    assert res.blockers[0].name == "Parse" and res.blockers[0].actor == "you"


def test_preflight_flags_zero_rows(monkeypatch):
    monkeypatch.setattr(src_mod, "_http_get", lambda url, **kw: (b"d,v\n", {}))
    res = HttpTabularProvider().preflight(_cfg())
    assert not res.ok  # reachable + parses, but zero rows is a blocker
    assert res.blockers[0].name == "Sample rows"


# ---- validation -----------------------------------------------------------


def test_validate_requires_url_and_schema():
    errs = HttpTabularProvider().validate(
        ConnectorConfig(name="f", kind="http-tabular", bronze_root="/tmp/b", params={})
    )
    assert any("--url" in e for e in errs) and any("--schema-ref" in e for e in errs)
