# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""A read of a renamed site sees the rows filed under its old name.

Surveying the node made the case for this concrete. `silver.signals` holds
28,792,315 rows under `site-d-old` and 2,934 under `site-d`;
`public.reactor_timeseries` holds 728,914,128 under `SITE-A-OLD` and none
under `site-a`. Rewriting the site column is a ~25 GB write on a node
with 57 GB free that had a disk-pressure outage the day before.

Widening the read is the same answer for no bytes moved: a query for the
canonical id matches the rows filed under either name, so the two are one
dataset. The rewrite becomes optional and can be batched whenever there is
headroom.
"""

from __future__ import annotations

import pytest

from axiom.extensions.builtins.data_platform.gold_query import (
    compile_where,
    parse_filter,
)
from axiom.infra.site_identity import SiteIdentities

COLUMNS = ["site", "site_id", "channel", "value", "ts"]


@pytest.fixture(autouse=True)
def declared(monkeypatch):
    from axiom.infra import site_identity

    registry = SiteIdentities()
    registry.declare("site-a", also_known_as=("SITE-A-OLD",))
    registry.declare("site-d", also_known_as=("site-d-old",))
    monkeypatch.setattr(site_identity, "_IDENTITIES", registry)
    return registry


def _where(expr):
    return compile_where(parse_filter(expr), COLUMNS)


class TestASiteEqualityIsWidened:
    def test_the_canonical_id_matches_the_old_rows(self):
        sql, params = _where("site = 'site-a'")
        assert sql == '"site" IN (%s, %s)'
        assert set(params) == {"SITE-A-OLD", "site-a"}

    def test_the_old_id_matches_the_new_rows_too(self):
        """Somebody with a saved query or a bookmark asks by the old name,
        and gets the whole site rather than only its history."""
        _sql, params = _where("site = 'SITE-A-OLD'")
        assert set(params) == {"SITE-A-OLD", "site-a"}

    def test_site_id_is_widened_as_well(self):
        _sql, params = _where("site_id = 'site-d'")
        assert set(params) == {"site-d-old", "site-d"}

    def test_an_in_list_widens_every_member(self):
        _sql, params = _where("site in ('site-a', 'site-d')")
        assert set(params) == {
            "SITE-A-OLD", "site-a", "site-d-old", "site-d"
        }

    def test_values_stay_bound_parameters(self):
        """A site named `'; --` must not become SQL. The widening happens
        in the parameter list, never in the statement."""
        sql, params = _where("site = 'site-a'")
        assert "site-a" not in sql
        assert "SITE-A-OLD" not in sql
        assert len(params) == sql.count("%s")


class TestWhatIsNotWidened:
    def test_a_site_with_no_former_names_is_left_exactly_as_it_was(self):
        """An `IN` with one mark is equivalent and needlessly different to
        read, so the SQL for the common case does not change at all."""
        sql, params = _where("site = 'site-b'")
        assert sql == '"site" = %s'
        assert params == ["site-b"]

    def test_another_column_is_untouched(self):
        sql, params = _where("channel = 'site-a'")
        assert sql == '"channel" = %s'
        assert params == ["site-a"]

    def test_an_inequality_on_site_is_not_rewritten(self):
        """`site > 'x'` is not a question about identity, and quietly
        rewriting it would be worse than not answering it."""
        sql, params = _where("site > 'a'")
        assert sql == '"site" > %s'
        assert params == ["a"]

    def test_nothing_declared_changes_nothing(self, monkeypatch):
        from axiom.infra import site_identity

        monkeypatch.setattr(site_identity, "_IDENTITIES", SiteIdentities())
        sql, params = _where("site = 'site-a'")
        assert sql == '"site" = %s'
        assert params == ["site-a"]


class TestFreshnessReportsOneSiteOnce:
    def test_a_renamed_site_is_reported_under_its_canonical_id(self, monkeypatch):
        """Reported under both names it reads as two sites, and both halves
        look stale: the old id stopped receiving and the new one has barely
        started."""
        from datetime import UTC, datetime

        from axiom.extensions.builtins.data_platform.skills import ingest_freshness

        class _Row:
            def __init__(self, site):
                self.site = site
                self.feed = "loop.instrument"
                self.source_class = "measured"
                self.n = 10
                # A timestamp, as the driver hands one back.
                self.last_ts = datetime(2026, 9, 23, tzinfo=UTC)

        class _Conn:
            def execute(self, _sql, _binds):
                return self

            def fetchall(self):
                return [_Row("site-d-old"), _Row("site-d")]

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        class _Engine:
            def connect(self):
                return _Conn()

        from axiom.infra import db

        monkeypatch.setattr(db, "engine_for", lambda _n: _Engine())
        result = ingest_freshness.run({"now": "2026-09-23T01:00:00Z"}, ctx=None)
        sites = {s["site"] for s in result.value["feeds"]}
        assert sites == {"site-d"}

    def test_the_query_asks_for_every_former_id(self, monkeypatch):
        from axiom.extensions.builtins.data_platform.skills import ingest_freshness

        seen = {}

        class _Conn:
            def execute(self, sql, binds):
                seen["sql"] = str(sql)
                seen["binds"] = binds
                return self

            def fetchall(self):
                return []

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        class _Engine:
            def connect(self):
                return _Conn()

        from axiom.infra import db

        monkeypatch.setattr(db, "engine_for", lambda _n: _Engine())
        ingest_freshness.run({"site": "site-a"}, ctx=None)
        assert set(seen["binds"]["sites"]) == {"SITE-A-OLD", "site-a"}
        assert "ANY(:sites)" in seen["sql"]
