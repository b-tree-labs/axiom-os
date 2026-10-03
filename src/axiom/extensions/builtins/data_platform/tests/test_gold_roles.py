# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Asking about a quantity rather than about a table.

Every other gold verb is keyed on table and column, and nobody asks a question
in those terms. A researcher asks about wall a temperature, a position, a power.
The only part of a signal that carries a quantity portably is its role: one
site was found carrying `XR`, `long_name_for_xr` and `Xr` for one instrument, so a
channel name is a local label and nothing more.
"""

from __future__ import annotations

from ..gold_query import compare, roles


class FakeCursor:
    """Returns each queued result in turn; records the SQL it was given."""

    def __init__(self, results):
        self._results = list(results)
        self.sql: list[str] = []
        self.params: list[list] = []
        self._current: list = []

    def execute(self, sql, params=None):
        self.sql.append(" ".join(str(sql).split()))
        self.params.append(list(params or []))
        self._current = self._results.pop(0) if self._results else []

    def fetchall(self):
        return self._current

    def fetchone(self):
        return self._current[0] if self._current else None


COLUMNS = [(n, "text") for n in ("site", "feed", "channel", "ts", "value",
                                 "unit", "role", "basis", "source_class")]


def _roles(rows):
    return roles(FakeCursor([COLUMNS, rows]))


class TestRolesAnswersWhatTheFleetCanBeAsked:
    def test_a_role_on_two_sites_in_one_unit_is_comparable(self):
        out = _roles([
            ("temperature_b", "site-d", "degC", 100),
            ("temperature_b", "site-e", "degC", 80),
        ])
        entry = out["data"]["roles"][0]
        assert entry["sites"] == ["site-d", "site-e"]
        assert entry["comparable"] is True
        assert "2 sites, all in degC" in entry["note"]

    def test_a_role_at_one_site_has_nothing_to_compare_with(self):
        out = _roles([("operating_mode", "ut", "", 5)])
        entry = out["data"]["roles"][0]
        assert entry["comparable"] is False
        assert "nothing to compare it with yet" in entry["note"]

    def test_the_row_counts_are_summed_per_site(self):
        out = _roles([
            ("fluid_temperature", "site-d", "degC", 100),
            ("fluid_temperature", "site-d", "degC", 50),
        ])
        assert out["data"]["roles"][0]["rows"] == 150


class TestTwoUnitsIsTwoQuestionsSharingAName:
    def test_it_is_not_comparable(self):
        out = _roles([
            ("power", "a", "W", 10),
            ("power", "b", "kW", 10),
        ])
        entry = out["data"]["roles"][0]
        assert entry["comparable"] is False
        assert entry["units"] == ["W", "kW"]
        assert "two questions sharing a name" in entry["note"]

    def test_a_stated_unit_beside_an_unstated_one_is_called_out(self):
        out = _roles([
            ("power", "a", "W", 10),
            ("power", "b", "", 10),
        ])
        entry = out["data"]["roles"][0]
        assert entry["comparable"] is False
        assert "stated unit with an unstated one" in entry["note"]

    def test_nobody_declaring_a_unit_is_its_own_message(self):
        out = _roles([("x", "a", "", 1), ("x", "b", "", 1)])
        assert "no site declares a unit" in out["data"]["roles"][0]["note"]


class TestItExcludesSelfTestsLikeEveryOtherVerb:
    def test_the_basis_predicate_is_in_the_query(self):
        cur = FakeCursor([COLUMNS, []])
        roles(cur)
        assert "basis" in cur.sql[-1]
        assert "selftest" in cur.params[-1]

    def test_and_is_dropped_when_asked(self):
        cur = FakeCursor([COLUMNS, []])
        roles(cur, include_synthetic=True)
        assert "selftest" not in cur.params[-1]


class TestItDoesNotTakeATable:
    def test_roles_live_on_signals_and_nowhere_else(self):
        """A table parameter would invite a caller to point this at something
        with no roles and get an empty answer that reads as "no data" rather
        than "wrong question"."""
        import inspect

        assert "table" not in inspect.signature(roles).parameters


def _compare(rows, **kw):
    cur = FakeCursor([COLUMNS, rows])
    return compare(cur, role="temperature_b", bucket="1 hour", **kw), cur


class TestCompareRefusesToOverlayTwoUnits:
    """A figure drawing kW against degC on one axis is a lie about
    comparability, and an aggregate over both is worse because the lie has no
    shape to notice."""

    MIXED = [("a", "degC", "t1", 20.0), ("b", "K", "t1", 293.0)]

    def test_it_returns_no_data_and_says_why(self):
        out, _ = _compare(self.MIXED)
        assert out["data"] is None
        assert "not true" in out["provenance"]["note"]
        assert "degC" in out["provenance"]["note"] and "K" in out["provenance"]["note"]

    def test_the_flag_stops_the_refusal_without_merging(self):
        """There is no correct way to merge them, and a flag should not
        pretend otherwise."""
        out, _ = _compare(self.MIXED, allow_mixed_units=True)
        assert set(out["data"]["by_unit"]) == {"degC", "K"}

    def test_one_unit_needs_no_flag(self):
        out, _ = _compare([("a", "degC", "t1", 20.0), ("b", "degC", "t1", 21.0)])
        assert set(out["data"]["by_unit"]) == {"degC"}
        assert set(out["data"]["by_unit"]["degC"]["sites"]) == {"a", "b"}

    def test_rows_with_no_unit_are_labelled_not_blanked(self):
        out, _ = _compare([("a", "", "t1", 1.0)], allow_mixed_units=True)
        assert "unit not declared" in out["data"]["by_unit"]


class TestCompareTheRestOfIt:
    def test_an_empty_window_says_so_rather_than_returning_nothing(self):
        out, _ = _compare([])
        assert out["data"] is None
        assert "no rows carry role" in out["provenance"]["note"]

    def test_the_role_is_bound_not_interpolated(self):
        _out, cur = _compare([])
        assert "temperature_b" in cur.params[-1]
        assert "temperature_b" not in cur.sql[-1]

    def test_sites_narrow_the_question(self):
        _out, cur = _compare([], sites=("site-d", "site-e"))
        assert "site-d" in cur.params[-1] and "site-e" in cur.params[-1]

    def test_a_bad_bucket_is_refused(self):
        import pytest

        from ..gold_query import FilterSyntaxError

        with pytest.raises(FilterSyntaxError):
            compare(FakeCursor([COLUMNS, []]), role="x", bucket="; drop table")

    def test_the_method_records_what_was_asked(self):
        out, _ = _compare([("a", "degC", "t1", 1.0)])
        assert "role = 'temperature_b'" in out["provenance"]["method"]
        assert "1 hour" in out["provenance"]["method"]
