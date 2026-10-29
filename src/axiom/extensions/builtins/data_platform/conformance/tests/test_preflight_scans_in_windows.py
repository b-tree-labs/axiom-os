# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""``reading-identity preflight`` and ``collapse`` never run one statement
over the whole table (ADR-206).

Seen live, 2026-10-27: a serving role with ``statement_timeout = 30s`` and a
160M-row plain ``silver.signals``; preflight's single GROUP BY over a 64M-row
site was cancelled and the operator got a raw traceback, twice. The scan now
runs one bounded query per window, each under its own ``SET LOCAL``
timeout, and a failing window is named in one clear line.

The fake cursor answers by SQL shape from an in-memory list of duplicate
groups, so the windowing logic is exercised without a server; one test at the
bottom runs the same scan against a real TimescaleDB.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import UTC, datetime
from types import SimpleNamespace

import psycopg
import pytest

from axiom.extensions.builtins.data_platform.conformance import reading_identity as ri
from axiom.extensions.builtins.data_platform.skills import reading_identity as skill


def _t(month: int, day: int, hour: int = 0) -> datetime:
    return datetime(2026, month, day, hour, tzinfo=UTC)


#: (site, feed, channel, ts, n rows, distinct contents). Straddles 1 Jun.
GROUPS = [
    ("s1", "f1", "T1", _t(5, 3), 2, 1),
    ("s1", "f1", "T1", _t(5, 31, 23), 3, 1),   # last hour of May
    ("s1", "f1", "T2", _t(6, 1), 2, 2),        # first instant of June
    ("s1", "f2", "T1", _t(6, 20), 5, 1),
    ("s2", "f1", "T1", _t(5, 15), 2, 1),
]


class FakeCur:
    def __init__(self, groups=GROUPS, catalogue=(_t(5, 1), _t(6, 25)), fail=None):
        self.groups, self.catalogue, self.fail = groups, catalogue, fail
        self.sql: list[str] = []
        self.dup_calls: list[dict] = []
        self.timeouts: list[str] = []
        self.connection = SimpleNamespace(rollback=lambda: None)
        self._rows: list = []
        self.description = None

    def _in(self, ts, p):
        return (p.get("start") is None or ts >= p["start"]) and (p.get("end") is None or ts < p["end"])

    def execute(self, sql, params=None):
        self.sql.append(sql)
        p = params if isinstance(params, dict) else {}
        if "set_config" in sql:
            assert ", true)" in sql, "statement_timeout must be LOCAL to the transaction"
            self.timeouts.append(params[0])
        elif "to_regclass" in sql:
            self._rows = [(self.catalogue is not None,)]
        elif "gold.signal_catalogue" in sql:
            self._rows = [self.catalogue]
        elif "min(ts), max(ts)" in sql:
            ts = [g[3] for g in self.groups if self._in(g[3], p) and p.get("site", g[0]) == g[0]]
            self._rows = [(min(ts), max(ts)) if ts else (None, None)]
        elif "GROUP BY site, feed" in sql:
            self.dup_calls.append(dict(p))
            if self.fail and self.fail(p):
                raise psycopg.errors.QueryCanceled("canceling statement due to statement timeout")
            agg: dict = {}
            for site, feed, _ch, ts, n, contents in self.groups:
                if not self._in(ts, p) or p.get("site", site) != site:
                    continue
                a = agg.setdefault((site, feed), [0, 0, 0, 0, 0, ts, ts])
                a[0] += 1
                a[1] += n - 1
                a[2] += contents == 1
                a[3] += contents > 1
                a[4] = max(a[4], n)
                a[5], a[6] = min(a[5], ts), max(a[6], ts)
            self.description = [(c,) for c in (
                "site", "feed", "identities", "extra_rows", "resend", "divergent", "worst", "first_ts", "last_ts")]
            self._rows = [(k[0], k[1], *v) for k, v in agg.items()]
        else:
            raise AssertionError(f"unexpected SQL: {sql}")

    def fetchone(self):
        return self._rows[0]

    def fetchall(self):
        return self._rows


@pytest.fixture(autouse=True)
def _no_layout(monkeypatch):
    monkeypatch.setattr(ri, "_layout", lambda cur: {"hypertable": False, "approximate_rows": 160_000_000})
    monkeypatch.setattr(ri, "has_identity_key", lambda cur: False)


def _by_feed(report):
    return {(d["site"], d["feed"]): d for d in report["duplicates"]}


def test_windows_merge_to_exactly_the_unwindowed_answer_across_a_boundary():
    one = ri.preflight(FakeCur(), window_days=3650)
    many = ri.preflight(FakeCur(), window_days=7)
    assert many["windows"] > one["windows"] == 1
    assert many["totals"] == one["totals"] == {"identities": 5, "extra_rows": 1 + 2 + 1 + 4 + 1, "resend": 4, "divergent": 1}
    assert many["duplicates"] == one["duplicates"], "ts is in the key: no identity spans two windows"
    f1 = _by_feed(many)[("s1", "f1")]
    assert (f1["identities"], f1["extra_rows"], f1["resend"], f1["divergent"], f1["worst"]) == (3, 4, 2, 1, 3)
    assert f1["first_ts"] == _t(5, 3).isoformat() and f1["last_ts"] == _t(6, 1).isoformat()


def test_each_window_is_bounded_and_runs_under_its_own_local_timeout():
    cur = FakeCur()
    ri.preflight(cur, window_days=31, statement_timeout_s=900)
    assert len(cur.dup_calls) == 2
    assert all("start" in c for c in cur.dup_calls)
    assert cur.dup_calls[0]["end"] == cur.dup_calls[1]["start"], "windows tile with no gap"
    assert set(cur.timeouts) == {"900000ms"} and len(cur.timeouts) >= len(cur.dup_calls)
    assert not any("ALTER" in s.upper() for s in cur.sql), "the role's setting is never changed"


def test_the_catalogue_gives_the_extent_and_the_scan_falls_back_without_it():
    with_cat = FakeCur()
    ri.preflight(with_cat, window_days=31)
    assert any("gold.signal_catalogue" in s and "min(first_ts)" in s for s in with_cat.sql)
    assert not any("min(ts), max(ts)" in s for s in with_cat.sql)
    assert "end" not in with_cat.dup_calls[-1], "a lagging catalogue never hides newer rows: last window is open"

    bare = FakeCur(catalogue=None)
    got = ri.preflight(bare, window_days=31)
    assert any("min(ts), max(ts)" in s for s in bare.sql)
    assert got["extent_source"] == "silver.signals" and got["totals"]["extra_rows"] == 9


def test_an_explicit_start_and_end_are_respected():
    cur = FakeCur()
    got = ri.preflight(cur, start=_t(5, 10), end=_t(6, 1), window_days=7)
    assert cur.dup_calls[0]["start"] == _t(5, 10) and cur.dup_calls[-1]["end"] == _t(6, 1)
    assert not any("min(ts), max(ts)" in s or "signal_catalogue" in s for s in cur.sql), "both given: no extent query"
    assert got["totals"]["extra_rows"] == 3, "only the 15 May and 31 May identities are in scope"
    assert _t(6, 1).isoformat() not in str(got["duplicates"]), "end is exclusive"


def test_one_progress_line_per_window():
    lines: list[str] = []
    ri.preflight(FakeCur(), window_days=31, progress=lines.append)
    assert lines == ["window 2026-05-01..2026-06-01: 4 extra", "window 2026-06-01..end: 5 extra"]


def test_a_timeout_in_one_window_names_it_and_suggests_a_smaller_window():
    cur = FakeCur(fail=lambda p: p["start"] == _t(6, 1))
    with pytest.raises(ri.WindowError) as err:
        ri.preflight(cur, window_days=31)
    msg = str(err.value)
    assert "window 2026-06-01..end" in msg and "timed out" in msg
    assert "--window-days" in msg and "try 15" in msg
    assert err.value.timed_out is True


def _patch_connect(monkeypatch, cur):
    conn = SimpleNamespace(rollback=lambda: None, commit=lambda: None)
    cur.connection = conn

    @contextmanager
    def cursor():
        yield cur

    conn.cursor = cursor

    @contextmanager
    def connect(*a, **k):
        yield conn

    monkeypatch.setattr(psycopg, "connect", connect)


def test_the_skill_returns_one_structured_error_instead_of_raising(monkeypatch, capsys):
    cur = FakeCur(fail=lambda p: p["start"] == _t(6, 1))
    _patch_connect(monkeypatch, cur)
    res = skill.run({"action": "preflight", "dsn": "postgresql://x/y"}, SimpleNamespace())
    assert res.ok is False and len(res.errors) == 1
    assert "window 2026-06-01..end" in res.errors[0] and "--window-days" in res.errors[0]
    err = capsys.readouterr().err
    assert "window 2026-05-01..2026-06-01: 4 extra" in err, "progress goes to stderr"
    assert "Traceback" not in err


def test_the_skill_reports_a_clean_run_with_windows_and_progress(monkeypatch, capsys):
    _patch_connect(monkeypatch, FakeCur())
    res = skill.run({"action": "preflight", "dsn": "postgresql://x/y", "window_days": 7,
                     "statement_timeout_s": 60}, SimpleNamespace())
    assert res.ok and res.value["windows"] > 2 and res.value["totals"]["extra_rows"] == 9
    assert capsys.readouterr().err.count("window ") == res.value["windows"]


def test_collapse_finds_its_extent_cheaply_and_names_a_failing_window():
    class Conn:
        def __init__(self, cur):
            self._cur = cur
            self.rolled = 0

        def cursor(self):
            return self

        def __enter__(self):
            return self._cur

        def __exit__(self, *a):
            return False

        def commit(self):
            pass

        def rollback(self):
            self.rolled += 1

    class CollapseCur(FakeCur):
        def execute(self, sql, params=None):
            if sql.lstrip().upper().startswith(("CREATE", "ALTER", "COMMENT", "DO")):
                return
            if "rn > 1 AND contents = 1" in sql:
                p = params
                if p["start"] == _t(5, 4):
                    raise psycopg.errors.QueryCanceled("canceling statement due to statement timeout")
                self._rows = [(0, 0, 0)]
                return
            super().execute(sql, params)

    cur = CollapseCur()
    conn = Conn(cur)
    cur.connection = conn
    lines: list[str] = []
    with pytest.raises(ri.WindowError) as err:
        ri.collapse(conn, site="s1", window_days=1, progress=lines.append)
    assert "window 2026-05-04..2026-05-05" in str(err.value)
    assert any("signal_catalogue" in s for s in cur.sql) and not any("min(ts), max(ts)" in s for s in cur.sql)
    assert lines[:3] == [f"window 2026-05-0{d}..2026-05-0{d + 1}: 0 would remove" for d in (1, 2, 3)]
    assert conn.rolled >= 1


def test_on_a_real_server_windows_add_up_and_the_session_timeout_is_untouched(timescale_dsn):
    from axiom.extensions.builtins.data_platform.conformance import SILVER_DDL

    with psycopg.connect(timescale_dsn, autocommit=True) as c:
        c.execute("DROP SCHEMA IF EXISTS gold CASCADE")
        c.execute("DROP SCHEMA IF EXISTS silver CASCADE")
        for stmt in SILVER_DDL:
            c.execute(stmt)
        c.execute("DROP INDEX IF EXISTS silver.signals_reading_identity")
        # The same reading twice (a re-send), different row_hash, on either side of 1 June.
        for i, ts in enumerate((_t(5, 31, 23), _t(6, 1), _t(6, 1, 1))):
            for h in ("a", "b"):
                c.execute(
                    "INSERT INTO silver.signals (site, feed, channel, ts, value, schema_ref, row_hash) "
                    "VALUES ('s1', 'f1', %s, %s, 1.0, 'x', %s)",
                    (f"T{i}", ts, f"{h}{i}"),
                )
    with psycopg.connect(timescale_dsn) as c, c.cursor() as cur:
        before = c.execute("SHOW statement_timeout").fetchone()[0]
        lines: list[str] = []
        windowed = ri.preflight(cur, site="s1", window_days=1, statement_timeout_s=60, progress=lines.append)
        whole = ri.preflight(cur, site="s1", window_days=3650)
        after = c.execute("SHOW statement_timeout").fetchone()[0]
    assert windowed["totals"] == whole["totals"] == {"identities": 3, "extra_rows": 3, "resend": 3, "divergent": 0}
    assert windowed["duplicates"] == whole["duplicates"]
    assert windowed["windows"] == 2 and len(lines) == 2
    assert before == after, "SET LOCAL ends with the window; the role's setting is never changed"
