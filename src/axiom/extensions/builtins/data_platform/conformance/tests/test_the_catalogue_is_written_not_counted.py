# Copyright (c) 2026 B-Tree Ventures, LLC
# SPDX-License-Identifier: Apache-2.0
"""What a site holds, written where it changes rather than counted on read.

Asking "what channels does this site have" was a GROUP BY over every row it
owns — 8.9 seconds on a 29.5-million-row site, paid by whoever opened a page,
for an answer that only changes when something is conformed.

These check the two things that make writing it safe: that a fold agrees with a
rebuild, and that a pass which wrote nothing moves nothing — because a revision
that ticks on an idempotent re-run busts every cache downstream for no reason.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from axiom.extensions.builtins.data_platform.conformance import catalogue

T0 = datetime(2026, 9, 1, tzinfo=UTC)


class _Cursor:
    """Enough Postgres to exercise the folding rules the SQL expresses."""

    def __init__(self, rows=None):
        self.table: dict = {}
        self.signals = rows or []
        self.clock = T0
        self.rowcount = 0
        self._result: list = []

    def _tick(self):
        self.clock += timedelta(seconds=1)
        return self.clock

    def execute(self, sql, params=None):
        flat = " ".join(sql.split())
        self._result = []
        if flat.startswith("CREATE SCHEMA"):
            return
        if flat.startswith("INSERT INTO gold.signal_catalogue (site") and "SELECT site" in flat:
            site = params[0]
            groups: dict = {}
            for r in self.signals:
                if r["site"] != site:
                    continue
                key = (r["site"], r["feed"], r["channel"])
                g = groups.setdefault(key, {"unit": None, "rows": 0, "first": None, "last": None})
                g["rows"] += 1
                g["unit"] = g["unit"] or r.get("unit")
                g["first"] = min(g["first"] or r["ts"], r["ts"])
                g["last"] = max(g["last"] or r["ts"], r["ts"])
            for key, g in groups.items():
                self.table[key] = {**g, "computed_at": self._tick()}
            self.rowcount = len(groups)
            return
        if flat.startswith("DELETE FROM gold.signal_catalogue"):
            site = params[0]
            live = {(r["site"], r["feed"], r["channel"]) for r in self.signals}
            for key in [k for k in self.table if k[0] == site and k not in live]:
                del self.table[key]
            return
        if flat.startswith("INSERT INTO gold.signal_catalogue"):
            key = (params["site"], params["feed"], params["channel"])
            existing = self.table.get(key)
            if existing is None:
                self.table[key] = {
                    "unit": params["unit"],
                    "rows": params["rows"],
                    "first": params["first"],
                    "last": params["last"],
                    "computed_at": self._tick(),
                }
            else:
                existing["unit"] = params["unit"] or existing["unit"]
                existing["rows"] += params["rows"]
                existing["first"] = min(existing["first"], params["first"])
                existing["last"] = max(existing["last"], params["last"])
                existing["computed_at"] = self._tick()
            return
        if flat.startswith("SELECT site, feed, channel, unit"):
            site = params[0]
            self._result = [
                (k[0], k[1], k[2], v["unit"], v["rows"], v["first"], v["last"], v["computed_at"])
                for k, v in sorted(self.table.items())
                if k[0] == site
            ]
            return
        if flat.startswith("SELECT max(computed_at)"):
            site = params[0]
            stamps = [v["computed_at"] for k, v in self.table.items() if k[0] == site]
            self._result = [(max(stamps) if stamps else None,)]
            return
        raise AssertionError("unhandled: " + flat[:70])

    def fetchall(self):
        return self._result

    def fetchone(self):
        return self._result[0] if self._result else None


def _written(feed="s", channel="c", unit="degC", rows=3, first=T0, last=None):
    return {
        "feed": feed,
        "channel": channel,
        "unit": unit,
        "rows": rows,
        "first": first,
        "last": last or (first + timedelta(hours=1)),
    }


class TestAFoldAgreesWithARebuild:
    def test_a_channel_conformed_once_reads_back(self):
        cur = _Cursor()
        catalogue.record(cur, "site", [_written()])
        got = catalogue.read(cur, "site")
        assert [(r.feed, r.channel, r.unit, r.rows) for r in got] == [("s", "c", "degC", 3)]

    def test_counts_accumulate_across_passes(self):
        cur = _Cursor()
        catalogue.record(cur, "site", [_written(rows=3)])
        catalogue.record(cur, "site", [_written(rows=4)])
        assert catalogue.read(cur, "site")[0].rows == 7

    def test_the_span_widens_at_both_ends(self):
        cur = _Cursor()
        catalogue.record(cur, "site", [_written(first=T0, last=T0 + timedelta(days=1))])
        catalogue.record(
            cur,
            "site",
            [
                _written(first=T0 - timedelta(days=5), last=T0 + timedelta(days=5)),
            ],
        )
        row = catalogue.read(cur, "site")[0]
        assert row.first == (T0 - timedelta(days=5)).isoformat()
        assert row.last == (T0 + timedelta(days=5)).isoformat()

    def test_a_span_never_narrows(self):
        """One that silently narrows hides that data went missing; one that is
        too wide is visibly a span."""
        cur = _Cursor()
        catalogue.record(cur, "site", [_written(first=T0, last=T0 + timedelta(days=10))])
        catalogue.record(
            cur, "site", [_written(first=T0 + timedelta(days=1), last=T0 + timedelta(days=2))]
        )
        assert catalogue.read(cur, "site")[0].last == (T0 + timedelta(days=10)).isoformat()

    def test_a_later_pass_with_no_unit_does_not_un_declare_one(self):
        """A single undeclared batch must not erase a unit an earlier pass
        established, or a channel silently loses its unit."""
        cur = _Cursor()
        catalogue.record(cur, "site", [_written(unit="degC")])
        catalogue.record(cur, "site", [_written(unit=None)])
        assert catalogue.read(cur, "site")[0].unit == "degC"

    def test_a_rebuild_reaches_the_same_answer(self):
        rows = [
            {
                "site": "site",
                "feed": "s",
                "channel": "c",
                "unit": "degC",
                "ts": T0 + timedelta(minutes=i),
            }
            for i in range(7)
        ]
        folded, rebuilt = _Cursor(), _Cursor(rows)
        for i in range(7):
            catalogue.record(
                folded,
                "site",
                [
                    _written(
                        rows=1, first=T0 + timedelta(minutes=i), last=T0 + timedelta(minutes=i)
                    ),
                ],
            )
        catalogue.rebuild(rebuilt, "site")
        a, b = catalogue.read(folded, "site")[0], catalogue.read(rebuilt, "site")[0]
        assert (a.rows, a.first, a.last, a.unit) == (b.rows, b.first, b.last, b.unit)


class TestTheRevisionOnlyMovesWhenSomethingDid:
    def test_a_write_moves_it(self):
        cur = _Cursor()
        before = catalogue.revision(cur, "site")
        catalogue.record(cur, "site", [_written()])
        assert catalogue.revision(cur, "site") != before

    def test_an_idempotent_re_run_does_not(self):
        """A conform pass that inserted nothing reports nothing. A revision
        that ticked anyway would bust every cache downstream for no reason —
        which is the failure a revision exists to avoid."""
        cur = _Cursor()
        catalogue.record(cur, "site", [_written()])
        settled = catalogue.revision(cur, "site")
        catalogue.record(cur, "site", [_written(rows=0)])
        assert catalogue.revision(cur, "site") == settled

    def test_nor_does_a_pass_that_wrote_nothing_at_all(self):
        cur = _Cursor()
        catalogue.record(cur, "site", [_written()])
        settled = catalogue.revision(cur, "site")
        assert catalogue.record(cur, "site", []) == 0
        assert catalogue.revision(cur, "site") == settled

    def test_a_site_nobody_has_conformed_has_no_revision(self):
        assert catalogue.revision(_Cursor(), "nobody") == ""

    def test_one_site_moving_does_not_move_another(self):
        cur = _Cursor()
        catalogue.record(cur, "a", [_written()])
        settled = catalogue.revision(cur, "a")
        catalogue.record(cur, "b", [_written()])
        assert catalogue.revision(cur, "a") == settled


class TestARebuildIsTheRepair:
    def test_it_drops_a_channel_that_no_longer_has_rows(self):
        """Left behind, it offers a picker something that draws nothing."""
        cur = _Cursor(
            rows=[{"site": "site", "feed": "s", "channel": "kept", "unit": "W", "ts": T0}]
        )
        catalogue.record(cur, "site", [_written(channel="gone")])
        catalogue.rebuild(cur, "site")
        assert [r.channel for r in catalogue.read(cur, "site")] == ["kept"]

    def test_it_replaces_a_count_rather_than_adding_to_it(self):
        cur = _Cursor(rows=[{"site": "site", "feed": "s", "channel": "c", "unit": "W", "ts": T0}])
        catalogue.record(cur, "site", [_written(rows=999)])
        catalogue.rebuild(cur, "site")
        assert catalogue.read(cur, "site")[0].rows == 1


class TestTheServedShape:
    def test_a_row_hands_out_what_a_lane_needs(self):
        cur = _Cursor()
        catalogue.record(cur, "site", [_written()])
        doc = catalogue.read(cur, "site")[0].as_document()
        assert set(doc) == {"feed", "channel", "unit", "rows", "first", "last"}

    def test_an_absent_unit_is_an_empty_string_not_a_null(self):
        cur = _Cursor()
        catalogue.record(cur, "site", [_written(unit=None)])
        assert catalogue.read(cur, "site")[0].as_document()["unit"] == ""


class TestConformAccumulatesItAsItWrites:
    """Building the catalogue in the conform loop is why it is nearly free:
    that loop already touches every row, and the alternative is a sequential
    scan of the whole table by whoever next opens a page."""

    def _counting(self, rowcounts):
        from axiom.extensions.builtins.data_platform.conformance.runner import (
            _by_site,
            _counting_upsert,
        )

        class _Cur:
            rowcount = 1

        cur = _Cur()
        seen = iter(rowcounts)

        def inner(_row):
            cur.rowcount = next(seen)

        upsert, counter = _counting_upsert(inner, cur)
        return upsert, counter, _by_site

    def _row(self, channel="c", unit="degC", ts=T0, site="s", feed="st"):
        return {"site": site, "feed": feed, "channel": channel, "unit": unit, "ts": ts}

    def test_it_collects_a_channel_as_the_rows_go_by(self):
        upsert, counter, by_site = self._counting([1, 1, 1])
        for i in range(3):
            upsert(self._row(ts=T0 + timedelta(minutes=i)))
        entry = by_site(counter)["s"][0]
        assert entry["rows"] == 3
        assert entry["first"] == T0 and entry["last"] == T0 + timedelta(minutes=2)

    def test_a_row_that_changed_NOTHING_is_not_counted(self):
        """An idempotent re-run accumulates nothing, so the revision does not
        move, so no cache downstream is busted by a run that did nothing."""
        upsert, counter, by_site = self._counting([0, 0])
        upsert(self._row())
        upsert(self._row())
        assert by_site(counter) == {}

    def test_one_row_without_a_unit_does_not_un_declare_the_batch(self):
        upsert, counter, by_site = self._counting([1, 1])
        upsert(self._row(unit="degC"))
        upsert(self._row(unit=None, ts=T0 + timedelta(minutes=1)))
        assert by_site(counter)["s"][0]["unit"] == "degC"

    def test_sites_are_kept_apart(self):
        upsert, counter, by_site = self._counting([1, 1])
        upsert(self._row(site="a"))
        upsert(self._row(site="b"))
        assert set(by_site(counter)) == {"a", "b"}

    def test_and_so_are_channels_within_one(self):
        upsert, counter, by_site = self._counting([1, 1])
        upsert(self._row(channel="x"))
        upsert(self._row(channel="y"))
        assert sorted(e["channel"] for e in by_site(counter)["s"]) == ["x", "y"]

    def test_what_it_collects_folds_into_the_catalogue(self):
        upsert, counter, by_site = self._counting([1, 1])
        upsert(self._row())
        upsert(self._row(ts=T0 + timedelta(hours=2)))
        cur = _Cursor()
        catalogue.record(cur, "s", by_site(counter)["s"])
        row = catalogue.read(cur, "s")[0]
        assert row.rows == 2 and row.unit == "degC"


class TestTheRunSaysWhatItWroteWithNoUnit:
    """Three channels at one site served unitless readings for a month because
    the only thing that could notice was a reader looking at a chart legend.

    The pass that wrote them knew: it had the rows in hand and had just read
    the map that should have named the unit. It said nothing.
    """

    def _entries(self):
        return {
            "site-a": [
                {
                    "feed": "loop.readings",
                    "channel": "ch-0",
                    "unit": "degC",
                    "rows": 3,
                    "first": T0,
                    "last": T0,
                },
                {
                    "feed": "loop.readings",
                    "channel": "ch-1",
                    "unit": "",
                    "rows": 3,
                    "first": T0,
                    "last": T0,
                },
                {
                    "feed": "loop.readings",
                    "channel": "ch-2",
                    "unit": None,
                    "rows": 3,
                    "first": T0,
                    "last": T0,
                },
            ],
            "site-b": [
                {
                    "feed": "console.readings",
                    "channel": "value",
                    "unit": "  ",
                    "rows": 1,
                    "first": T0,
                    "last": T0,
                },
            ],
        }

    def test_it_names_them_rather_than_counting_them(self):
        from axiom.extensions.builtins.data_platform.conformance.runner import _undeclared

        got = _undeclared(self._entries())
        assert got["site-a"] == ["loop.readings:ch-1", "loop.readings:ch-2"]

    def test_a_unit_of_whitespace_is_not_a_unit(self):
        from axiom.extensions.builtins.data_platform.conformance.runner import _undeclared

        got = _undeclared(self._entries())
        assert got["site-b"] == ["console.readings:value"]

    def test_a_site_where_everything_declares_one_is_not_listed(self):
        from axiom.extensions.builtins.data_platform.conformance.runner import _undeclared

        clean = {
            "site-c": [
                {
                    "feed": "epics.readings",
                    "channel": "PT41",
                    "unit": "bar",
                    "rows": 1,
                    "first": T0,
                    "last": T0,
                },
            ]
        }
        assert _undeclared(clean) == {}
