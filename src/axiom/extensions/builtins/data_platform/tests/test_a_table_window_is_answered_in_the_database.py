# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""``data.table`` and ``data.profile`` over a real gold relation (ADR-217).

What is proven here, against Postgres rather than by example:

* **Pushdown.** Every statement the door ran is EXPLAINed with its own
  parameters: the order comes from an index scan (no Sort node over the
  result), and the ``where`` is an index condition or a filter in the plan.
* **Keyset.** Paging by cursor through a sorted, filtered result returns exactly
  the rows, in exactly the order, that sorting everything would (nulls last,
  both directions, ties broken by the row key), and no cursor page uses OFFSET.
* **No sort window.** The first row of a descending sort is the maximum of the
  whole relation, which sits far past the 10,000 rows 1.0 sorted in memory.
* Counts, bounds, gold-only, the site scope, row-level security, the
  sortable-means-indexed conformance check, and the profile's sample label and
  watermark cache.
"""

from __future__ import annotations

import json
import uuid

import pytest

from axiom.extensions.builtins.data_platform import gold_query as gq
from axiom.extensions.builtins.data_platform import table_door as td
from axiom.extensions.builtins.scidisplay import table_spec as ts

psycopg2 = pytest.importorskip("psycopg2")

TABLE = f"table_door_{uuid.uuid4().hex[:8]}"
ROWS = 200_000  # 20 series x 10,000 readings: past the 10,000-row window 1.0 sorted in memory


def _libpq(url: str) -> str:
    from axiom.infra.db import libpq_url

    return libpq_url(url)


@pytest.fixture(scope="module")
def db(_scratch_postgres_url):
    dsn = _libpq(_scratch_postgres_url)
    owner = psycopg2.connect(dsn)
    owner.autocommit = True
    c = owner.cursor()
    c.execute("CREATE SCHEMA IF NOT EXISTS gold")
    c.execute(f"""
        CREATE TABLE gold.{TABLE} (
          site text NOT NULL, channel text NOT NULL, ts timestamptz NOT NULL,
          value double precision, sigma double precision, unit text,
          quality text NOT NULL, quality_reason text,
          basis text NOT NULL DEFAULT 'live', access_tier text NOT NULL DEFAULT 'public')""")
    # Deterministic values with ties; a null every 997th row; a bad sentinel 0 every 1009th;
    # one selftest row the platform must hold out; the maximum placed in the last series.
    c.execute(f"""
        INSERT INTO gold.{TABLE}
        SELECT s.site, 'c' || lpad(ch::text, 2, '0'),
               timestamptz '2026-10-01' + make_interval(secs => i),
               CASE WHEN n % 997 = 0 THEN NULL WHEN n % 1009 = 0 THEN 0
                    ELSE ((n * 7919) % 100003) / 100.0 END,
               CASE WHEN ch = 1 THEN NULL ELSE 0.5 END,
               CASE WHEN ch = 3 THEN NULL ELSE 'kW' END,
               CASE WHEN n % 1009 = 0 THEN 'bad' WHEN n % 503 = 0 THEN 'suspect' ELSE 'good' END,
               CASE WHEN n % 1009 = 0 THEN 'sentinel:0' WHEN n % 503 = 0 THEN 'consistent:x' END,
               'live', 'public'
        FROM (VALUES ('site-a', 0), ('site-b', 1)) s(site, k)
        CROSS JOIN generate_series(1, 10) ch
        CROSS JOIN generate_series(0, 9999) i
        CROSS JOIN LATERAL (SELECT (s.k * 10 + ch - 1) * 10000 + i AS n) nn""")
    c.execute(f"UPDATE gold.{TABLE} SET value = 5000 WHERE site = 'site-b' AND channel = 'c10' AND ts = "
              f"timestamptz '2026-10-01' + interval '9990 seconds'")
    c.execute(f"UPDATE gold.{TABLE} SET basis = 'selftest', value = 99999 WHERE site = 'site-a' "
              f"AND channel = 'c01' AND ts = timestamptz '2026-10-01'")
    c.execute(f"CREATE UNIQUE INDEX {TABLE}_key ON gold.{TABLE} (site, channel, ts)")
    c.execute(f"CREATE INDEX {TABLE}_ts ON gold.{TABLE} (ts, site, channel)")
    c.execute(f"CREATE INDEX {TABLE}_value ON gold.{TABLE} (value)")
    # Mixed directions need an index stored that way: (site ASC, ts DESC).
    c.execute(f"CREATE INDEX {TABLE}_site_ts ON gold.{TABLE} (site, ts DESC)")
    c.execute(f"ANALYZE gold.{TABLE}")
    reader = psycopg2.connect(dsn)
    reader.set_session(readonly=True, autocommit=True)
    yield {"dsn": dsn, "owner": owner, "reader": reader}
    reader.close()
    c.execute(f"DROP TABLE IF EXISTS gold.{TABLE} CASCADE")
    owner.close()


class Recording:
    """A cursor that keeps every statement it ran, with its parameters, for EXPLAIN."""

    def __init__(self, cur):
        self._cur = cur
        self.calls: list[tuple[str, list]] = []

    def execute(self, sql, params=None):
        self.calls.append((sql, list(params or [])))
        return self._cur.execute(sql, params)

    def __getattr__(self, name):
        return getattr(self._cur, name)

    def windows(self):
        return [(s, p) for s, p in self.calls
                if s.startswith("SELECT ") and 'FROM "gold".' in s and " ORDER BY " in s]


def _spec(sort=None, where=None, window=None, columns=None, **source_args):
    doc = {
        "schema_version": "2.0", "kind": "rows", "title": "Readings",
        "source": {"type": "verb", "verb": "data.table", "args": {"dataset": TABLE, **source_args}},
        "columns": columns or [
            {"id": "ts", "label": "Time", "sortable": True, "type": "time"},
            {"id": "site", "label": "Site", "sortable": True},
            {"id": "channel", "label": "Channel", "sortable": True},
            {"id": "value", "label": "Value", "sortable": True, "type": "quantity", "unit": "kW",
             "uncertainty_from": "sigma"},
            {"id": "quality", "label": "Quality", "sortable": True, "type": "quality"},
        ],
        "window": window or {"offset": 0, "limit": 200, "cursor": None},
    }
    if sort:
        doc["sort"] = sort
    if where:
        doc["filter"] = {"where": where}
    return ts.parse_document(doc)


def _plan_nodes(cur, sql, params):
    cur.execute("EXPLAIN (FORMAT JSON) " + sql, params)
    plan = cur.fetchone()[0]
    plan = json.loads(plan) if isinstance(plan, str) else plan
    out = []

    def walk(node):
        out.append(node)
        for child in node.get("Plans", []):
            walk(child)

    walk(plan[0]["Plan"])
    return out


def _all_rows(db, where_sql="basis <> 'selftest'"):
    cur = db["reader"].cursor()
    cur.execute(f"SELECT site, channel, ts, value FROM gold.{TABLE} WHERE {where_sql}")
    return cur.fetchall()


class TestTheWorkGoesToTheDatabase:
    @pytest.mark.parametrize("sort, index, direction", [
        ([{"column": "ts", "direction": "desc"}], "_ts", "Backward"),
        ([{"column": "ts", "direction": "asc"}], "_ts", "Forward"),
        ([{"column": "value", "direction": "desc"}], "_value", "Backward"),
        ([{"column": "value", "direction": "asc"}], "_value", "Forward"),
        (None, "_key", "Forward"),
    ])
    def test_the_order_is_an_index_scan_and_nothing_sorts_the_result(self, db, sort, index, direction):
        rec = Recording(db["reader"].cursor())
        td.answer(rec, _spec(sort))
        (sql, params), *_ = rec.windows()
        assert "OFFSET" not in sql
        nodes = _plan_nodes(db["reader"].cursor(), sql, params)
        types = [n["Node Type"] for n in nodes]
        assert nodes[0]["Node Type"] == "Limit"
        assert "Sort" not in types, types  # an Incremental Sort over ties is allowed; a full sort is not
        scans = [n for n in nodes if n["Node Type"] in ("Index Scan", "Index Only Scan")]
        assert scans and scans[0]["Index Name"].endswith(index)
        assert scans[0]["Scan Direction"] == direction

    def test_where_compiles_to_an_index_condition_with_bound_values(self, db):
        rec = Recording(db["reader"].cursor())
        td.answer(rec, _spec([{"column": "value", "direction": "asc"}],
                             [{"column": "value", "op": "between", "value": [10, 20]}]))
        sql, params = rec.windows()[0]
        # The table name is random hex and can contain the digits.
        assert "10" not in sql.replace(TABLE, "") and 10 in params  # values are parameters, never SQL text
        nodes = _plan_nodes(db["reader"].cursor(), sql, params)
        cond = " ".join(n.get("Index Cond", "") for n in nodes)
        assert "value" in cond and ">=" in cond and "<=" in cond

    def test_a_filter_on_an_unindexed_column_is_still_in_the_statement(self, db):
        rec = Recording(db["reader"].cursor())
        out = td.answer(rec, _spec([{"column": "ts", "direction": "desc"}],
                                   [{"column": "quality", "op": "in", "value": ["bad"]}]))
        assert all(r["quality"] == "bad" for r in out["rows"]) and out["rows"]
        nodes = _plan_nodes(db["reader"].cursor(), *rec.windows()[0])
        assert any("quality" in n.get("Filter", "") for n in nodes)

    def test_a_cursor_page_seeks_by_the_index_never_by_offset(self, db):
        cur = db["reader"].cursor()
        first = td.answer(cur, _spec([{"column": "ts", "direction": "desc"}]))
        rec = Recording(db["reader"].cursor())
        td.answer(rec, _spec([{"column": "ts", "direction": "desc"}],
                             window={"offset": 0, "limit": 200, "cursor": first["next"]}))
        sql, params = rec.windows()[0]
        assert "OFFSET" not in sql
        nodes = _plan_nodes(db["reader"].cursor(), sql, params)
        assert "Sort" not in [n["Node Type"] for n in nodes]
        assert any("ts" in n.get("Index Cond", "") for n in nodes)

    def test_no_sort_window_the_first_descending_row_is_the_relations_maximum(self, db, monkeypatch):
        # 1.0's in-memory path must not be touched by a door source at all.
        def refuse(*a, **k):
            raise AssertionError("a door source used the in-memory sort window")

        monkeypatch.setattr(ts, "rows_needed", refuse)
        monkeypatch.setattr(ts, "tabulate", refuse)
        out = td.answer(db["reader"].cursor(), _spec([{"column": "value", "direction": "desc"}]))
        top = out["rows"][0]
        assert top["value"] == 5000 and top["site"] == "site-b" and top["channel"] == "c10"
        # ...and the selftest row (99999) was held out, as every gold verb holds it out.
        assert "basis != 'selftest'" in out["applied"]["scope"]


def _page_all(db, sort, where=None, limit=997):
    cur = db["reader"].cursor()
    got, cursor, pages = [], None, 0
    while True:
        out = td.answer(cur, _spec(sort, where, window={"offset": 0, "limit": limit, "cursor": cursor}))
        got += [(r["site"], r["channel"], r["ts"], r["value"]) for r in out["rows"]]
        cursor, pages = out["next"], pages + 1
        if not cursor:
            return got, pages


class TestKeysetIsExact:
    @pytest.mark.parametrize("direction", ["asc", "desc"])
    def test_paging_a_nullable_sort_equals_sorting_everything(self, db, direction):
        where = [{"column": "channel", "op": "in", "value": ["c02", "c07"]}]
        got, pages = _page_all(db, [{"column": "value", "direction": direction}], where)
        rows = _all_rows(db, "basis <> 'selftest' AND channel IN ('c02', 'c07')")
        desc = direction == "desc"
        present = sorted((r for r in rows if r[3] is not None),
                         key=lambda r: (r[3], r[0], r[1], r[2]), reverse=desc)
        nulls = sorted((r for r in rows if r[3] is None), key=lambda r: (r[0], r[1], r[2]), reverse=desc)
        expected = [(s, c, t.isoformat(), v) for s, c, t, v in present + nulls]
        assert pages > 30
        assert got == expected  # nulls last both ways; ties broken by the row key

    def test_a_mixed_direction_sort_needs_an_index_stored_that_way(self, db):
        rec = Recording(db["reader"].cursor())
        td.answer(rec, _spec([{"column": "site", "direction": "asc"}, {"column": "ts", "direction": "desc"}]))
        nodes = _plan_nodes(db["reader"].cursor(), *rec.windows()[0])
        assert "Sort" not in [n["Node Type"] for n in nodes]
        assert any(n.get("Index Name", "").endswith("_site_ts") for n in nodes)

    def test_a_mixed_direction_multi_key_sort_pages_exactly(self, db):
        where = [{"column": "ts", "op": "lt", "value": "2026-10-01T00:20:00+00:00"}]
        got, _ = _page_all(db, [{"column": "site", "direction": "asc"}, {"column": "ts", "direction": "desc"}],
                           where, limit=113)
        rows = _all_rows(db, "basis <> 'selftest' AND ts < '2026-10-01T00:20:00+00:00'")
        rows.sort(key=lambda r: (r[0], -r[2].timestamp(), r[1]), reverse=False)
        # ties on (site, ts) broken by channel in the LAST key's direction (desc)
        rows.sort(key=lambda r: r[1], reverse=True)
        rows.sort(key=lambda r: r[2], reverse=True)
        rows.sort(key=lambda r: r[0])
        assert got == [(s, c, t.isoformat(), v) for s, c, t, v in rows]

    def test_a_cursor_from_another_query_is_refused(self, db):
        cur = db["reader"].cursor()
        out = td.answer(cur, _spec([{"column": "ts", "direction": "desc"}]))
        with pytest.raises(td.TableDoorError, match="different sort or filter"):
            td.answer(cur, _spec([{"column": "value", "direction": "desc"}],
                                 window={"offset": 0, "limit": 10, "cursor": out["next"]}))

    def test_a_large_offset_is_an_estimated_jump_and_says_so(self, db):
        rec = Recording(db["reader"].cursor())
        out = td.answer(rec, _spec([{"column": "ts", "direction": "asc"}],
                                   window={"offset": 150_000, "limit": 50, "cursor": None}))
        assert out["position"]["kind"] == "estimate"
        assert out["position"]["method"] == "planner histogram"
        assert any("jumped to about row 150,000" in b for b in out["bounded"])
        assert all("OFFSET" not in s for s, _ in rec.windows())
        # roughly three quarters of the way through 10,000 seconds
        assert "2026-10-01T01:5" in out["rows"][0]["ts"] or "2026-10-01T02:" in out["rows"][0]["ts"]

    def test_a_small_offset_is_exact(self, db):
        cur = db["reader"].cursor()
        a = td.answer(cur, _spec([{"column": "ts", "direction": "asc"}], window={"offset": 0, "limit": 60, "cursor": None}))
        b = td.answer(cur, _spec([{"column": "ts", "direction": "asc"}], window={"offset": 40, "limit": 20, "cursor": None}))
        assert b["position"] == {"kind": "exact", "offset": 40}
        assert b["keys"] == a["keys"][40:60]


class TestCountsAreHonest:
    def test_a_small_result_is_counted_exactly(self, db):
        out = td.answer(db["reader"].cursor(), _spec(where=[{"column": "channel", "op": "eq", "value": "c04"}]))
        assert out["count"]["kind"] == "exact" and out["count"]["n"] == 20_000

    def test_a_large_one_is_an_estimate_labelled_as_one(self, db, monkeypatch):
        monkeypatch.setattr(td, "EXACT_COUNT_THRESHOLD", 1_000)
        out = td.answer(db["reader"].cursor(), _spec())
        assert out["count"]["kind"] == "estimate"
        assert abs(out["count"]["n"] - ROWS) / ROWS < 0.2
        assert "planner" in out["count"]["method"]

    def test_exact_on_request_is_its_own_bounded_call(self, db, monkeypatch):
        monkeypatch.setattr(td, "EXACT_COUNT_THRESHOLD", 1_000)
        out = td.answer(db["reader"].cursor(), _spec(), exact_count=True)
        assert out["count"] == {"kind": "exact", "n": ROWS - 1, "method": "count(*)"}

    def test_an_exact_count_that_runs_out_of_time_stays_an_estimate_and_says_why(self, db):
        q = td.build_query(td.load_relation(db["reader"].cursor(), TABLE), _spec())
        got = td.count_rows(db["reader"].cursor(), q, exact=True, timeout_ms=1)
        assert got["kind"] == "estimate" and "did not finish within" in got["reason"]


class TestEveryCallIsBounded:
    def test_the_row_cap_and_the_callers_budget(self, db):
        out = td.answer(db["reader"].cursor(), _spec(window={"offset": 0, "limit": 1000, "cursor": None}),
                        max_points=50, caller_class="agent")
        assert len(out["rows"]) == 50
        assert any("limited to 50 rows" in b for b in out["bounded"])

    def test_a_window_over_the_cost_budget_is_refused_with_the_reason(self, db, monkeypatch):
        monkeypatch.setitem(td.COST_BUDGET, "interactive", 0.001)
        with pytest.raises(td.TableDoorError, match="over the budget"):
            td.answer(db["reader"].cursor(), _spec([{"column": "ts", "direction": "desc"}]))

    def test_a_sort_the_database_cannot_serve_is_refused_naming_the_index(self, db):
        with pytest.raises(td.TableDoorError, match=r"no index starts with quality.*An index on \(quality\)"):
            td.answer(db["reader"].cursor(), _spec([{"column": "quality", "direction": "asc"}]))

    def test_grouping_is_refused_until_phase_four(self, db):
        doc = _spec().to_document() | {"group": {"by": ["site"], "aggregates": []}}
        with pytest.raises(td.TableDoorError, match="phase 4"):
            td.answer(db["reader"].cursor(), ts.parse_document(doc))


class TestARefusalSaysWhatWouldSucceedAsData:
    """The sentence is for a person; ``hints`` is the same advice for a table to offer in one click."""

    def test_a_slow_filter_is_refused_and_offers_to_remove_exactly_it(self, db):
        where = [{"column": "quality", "op": "eq", "value": "bad"},
                 {"column": "site", "op": "eq", "value": "site-a"}]
        with pytest.raises(td.TableDoorError, match="did not finish") as e:
            td.answer(db["reader"].cursor(), _spec(where=where), timeout_ms=1)
        removes = [h for h in e.value.hints if h["action"] == "remove_filter"]
        # quality leads no index, site does: the filter that made it slow is the one offered.
        assert [h["filter"] for h in removes] == [{"column": "quality", "op": "eq", "value": "bad"}]
        assert removes[0]["label"] == "Remove filter quality eq bad"

    def test_an_unindexed_sort_offers_an_indexed_column_in_the_same_direction(self, db):
        with pytest.raises(td.TableDoorError, match="no index starts with quality") as e:
            td.answer(db["reader"].cursor(), _spec([{"column": "quality", "direction": "desc"}]))
        sorts = [h for h in e.value.hints if h["action"] == "sort_by"]
        assert sorts and sorts[0]["column"] == "ts" and sorts[0]["direction"] == "desc"
        assert sorts[0]["label"] == "Sort by Time"

    def test_a_profile_refusal_offers_the_filter_to_remove(self, db):
        with pytest.raises(td.TableDoorError) as e:
            td.profile(db["reader"].cursor(), _spec(where=[{"column": "quality", "op": "eq", "value": "bad"}]),
                       "value", timeout_ms=1)
        assert [h["action"] for h in e.value.hints] == ["remove_filter"]

    def test_the_http_envelope_carries_them_in_unmet(self, db, monkeypatch):
        fastapi = pytest.importorskip("fastapi")
        from fastapi.testclient import TestClient

        from axiom.extensions.builtins.data_platform import read_api
        from axiom.extensions.builtins.data_platform.skills import gold

        monkeypatch.setattr(read_api, "holds_medallion", lambda: True)

        def fake(params, ctx):
            try:
                with db["reader"].cursor() as cur:
                    td.answer(cur, _spec([{"column": "quality", "direction": "asc"}]))
            except gq.GoldQueryError as exc:
                hints = list(exc.hints)
                return gold.SkillResult(ok=False, errors=[str(exc)], unmet=[{
                    "id": "table.refused", "why": str(exc), "fix": hints[0]["label"], "hints": hints}])

        monkeypatch.setattr(gold, "table", fake)
        app = fastapi.FastAPI()
        app.include_router(read_api.build_data_read_router(), prefix="/api/v1")
        body = TestClient(app).post("/api/v1/data/query", json={"verb": "data.table", "params": {}}).json()
        assert body["ok"] is False and body["errors"][0].startswith("refused: sorting")
        assert body["unmet"][0]["hints"][0] == {
            "action": "sort_by", "column": "ts", "direction": "asc", "label": "Sort by Time"}


class TestGoldOnlyAndTenancy:
    def test_only_gold_is_reachable(self, db):
        c = db["owner"].cursor()
        c.execute("CREATE SCHEMA IF NOT EXISTS silver")
        c.execute(f"CREATE TABLE IF NOT EXISTS silver.{TABLE}_s (a int)")
        try:
            with pytest.raises(gq.UnknownObject, match="reads gold only"):
                td.answer(db["reader"].cursor(), ts.parse_document(
                    _spec().to_document() | {"source": {"type": "verb", "verb": "data.table",
                                                        "args": {"dataset": f"{TABLE}_s"}},
                                             "columns": [{"id": "a", "label": "A"}]}))
        finally:
            c.execute(f"DROP TABLE silver.{TABLE}_s")

    def test_a_site_bound_caller_reads_only_its_site(self, db, monkeypatch):
        import axiom.infra.site_identity as sid

        monkeypatch.setattr(sid, "resolve", lambda s: s)
        out = td.answer(db["reader"].cursor(), _spec(), site_scope="site-b")
        assert {r["site"] for r in out["rows"]} == {"site-b"}
        assert any("held to the credential's site" in s for s in out["applied"]["scope"])

    def test_row_level_security_holds_underneath(self, db):
        role = f"reader_{uuid.uuid4().hex[:8]}"
        c = db["owner"].cursor()
        c.execute(f'CREATE ROLE "{role}" NOLOGIN')
        c.execute(f'GRANT USAGE ON SCHEMA gold TO "{role}"')
        c.execute(f'GRANT SELECT ON gold.{TABLE} TO "{role}"')
        c.execute(f"ALTER TABLE gold.{TABLE} ENABLE ROW LEVEL SECURITY")
        c.execute(f'CREATE POLICY "{role}_rows" ON gold.{TABLE} FOR SELECT TO "{role}" USING (site = \'site-a\')')
        conn = psycopg2.connect(db["dsn"])
        try:
            conn.set_session(readonly=True, autocommit=True)
            cur = conn.cursor()
            cur.execute(f'SET ROLE "{role}"')
            out = td.answer(cur, _spec([{"column": "ts", "direction": "desc"}]))
            assert out["rows"] and {r["site"] for r in out["rows"]} == {"site-a"}
        finally:
            conn.close()
            c.execute(f'DROP POLICY "{role}_rows" ON gold.{TABLE}')
            c.execute(f"ALTER TABLE gold.{TABLE} DISABLE ROW LEVEL SECURITY")
            c.execute(f'REVOKE ALL ON gold.{TABLE} FROM "{role}"')
            c.execute(f'REVOKE ALL ON SCHEMA gold FROM "{role}"')
            c.execute(f'DROP ROLE "{role}"')


class TestTheEnvelope:
    def test_applied_reads_and_uncertainty(self, db):
        out = td.answer(db["reader"].cursor(), _spec([{"column": "ts", "direction": "desc"}],
                                                     [{"column": "site", "op": "eq", "value": "site-a"}]))
        assert out["applied"]["sort"] == [{"column": "ts", "direction": "desc", "nulls": "last"}]
        assert out["applied"]["tiebreak"] == ["site", "channel"]
        assert out["applied"]["filter"] == {"where": [{"column": "site", "op": "eq", "value": "site-a"}]}
        read = out["provenance"]["reads"][0]
        assert read["tool"] == "data.table" and read["relation"] == f"gold.{TABLE}" and read["rows"] == 200
        assert out["provenance"]["watermark"] and out["provenance"]["as_of"]
        unc = out["uncertainty"]
        assert set(unc) == {"kind", "claimable", "columns", "note"}
        assert unc["columns"]["value"]["from"] == "sigma"
        assert unc["columns"]["value"]["quantified"] + unc["columns"]["value"]["unquantified"] > 0
        assert len(out["keys"]) == len(set(out["keys"])) == 200


class TestSortableMeansIndexedOrDeclaredCheap:
    def test_the_conformance_check_fails_an_unindexed_sortable_column(self, db):
        cur = db["reader"].cursor()
        bad = td.TableSourceDeclaration(table=TABLE, sortable=("ts", "value", "quality"))
        failures = td.check_declarations(cur, [bad])
        assert len(failures) == 1 and "quality" in failures[0]

    def test_declared_cheap_passes(self, db):
        ok = td.TableSourceDeclaration(table=TABLE, sortable=("ts", "quality"), cheap=("quality",))
        assert td.check_declarations(db["reader"].cursor(), [ok]) == []

    def test_every_declared_table_source_conforms(self, db):
        """The registry itself: whatever a deployment declares must pass, here or on its node."""
        failures = [f for f in td.check_declarations(db["reader"].cursor())
                    if "no table gold." not in f]  # a declaration for a relation this database lacks
        assert failures == []


class TestTheProfile:
    def test_a_small_filter_is_profiled_exactly(self, db):
        out = td.profile(db["reader"].cursor(), _spec(where=[{"column": "channel", "op": "eq", "value": "c05"}]),
                         "value")
        assert out["basis"]["kind"] == "exact" and out["n"] == 20_000
        assert out["histogram"]["counts"] and sum(out["histogram"]["counts"]) == round(20_000 * (1 - out["nulls"]))
        assert len(out["quantiles"]) == 5 and out["flags"]["bad"] > 0

    def test_a_time_column_profiles_as_times(self, db):
        out = td.profile(db["reader"].cursor(), _spec(where=[{"column": "channel", "op": "eq", "value": "c05"}]), "ts")
        assert out["min"].startswith("2026-10-01T00:00:00") and out["max"].startswith("2026-10-01T02:46:39")
        assert sum(out["histogram"]["counts"]) == 20_000

    def test_above_the_threshold_it_is_a_labelled_sample(self, db):
        out = td.profile(db["reader"].cursor(), _spec(), "quality", sample_threshold=10_000)
        assert out["basis"]["kind"] == "sample" and out["basis"]["label"].startswith("sample of ")
        assert out["distinct"]["kind"] == "estimate" and out["n_kind"] == "estimate"
        assert out["top"][0]["value"] == "good"

    def test_a_profile_that_runs_out_of_time_is_refused_with_the_reason(self, db):
        with pytest.raises(td.TableDoorError, match="did not finish within .* narrow it by an indexed column"):
            td.profile(db["reader"].cursor(), _spec(where=[{"column": "quality", "op": "eq", "value": "bad"}]),
                       "value", timeout_ms=1)

    def test_cached_by_query_and_watermark(self, db):
        cur = db["reader"].cursor()
        spec = _spec(where=[{"column": "channel", "op": "eq", "value": "c06"}])
        first = td.profile(cur, spec, "value")
        again = td.profile(cur, spec, "value")
        assert first["cache"]["hit"] is False and again["cache"]["hit"] is True
        c = db["owner"].cursor()
        c.execute(f"INSERT INTO gold.{TABLE} (site, channel, ts, value, quality) VALUES "
                  f"('site-a', 'c06', '2026-10-02', 1, 'good')")
        c.execute("SELECT pg_stat_force_next_flush()")
        try:
            after = td.profile(cur, spec, "value")
            assert after["cache"]["hit"] is False and after["n"] == first["n"] + 1
        finally:
            c.execute(f"DELETE FROM gold.{TABLE} WHERE ts = '2026-10-02'")
