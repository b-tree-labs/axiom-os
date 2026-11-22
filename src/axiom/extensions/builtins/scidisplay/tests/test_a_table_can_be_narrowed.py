# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""A table can be narrowed, and says how hard it narrowed.

Page, sort, direction — and no way to filter. The UT console deposit is
6,300 rows across seven channels: two hundred and fifty-two pages, and a
reader wanting one channel had to page to it. The chart verb beside it took
`--channels` and did exactly this.

The filter is part of the SPEC rather than something a caller does to the
rows first, for the reason `sort` is: a table showing a hundredth of its
rows and not saying so is a different table, and the document is what says
which one it is.
"""

from __future__ import annotations

import pytest

from axiom.extensions.builtins.scidisplay.table_spec import (
    Column,
    Filter,
    SpecFormatError,
    TableSpec,
    UnknownFieldError,
    parse_document,
    tabulate,
)

COLUMNS = (("channel", "channel", True, "left"),
           ("value", "value", True, "right"),
           ("unit", "unit", True, "left"))

ROWS = [{"channel": "power", "value": 950.0, "unit": "W"},
        {"channel": "fuel_temp_1", "value": 260.7, "unit": "degC"},
        {"channel": "fuel_temp_2", "value": 256.2, "unit": "degC"},
        {"channel": "rod_shim_1", "value": 745.0, "unit": "step"}]


def _view(**kw):
    return tabulate(ROWS, columns=COLUMNS, title="channels", **kw)


class TestExactMatch:
    def test_one_column_narrows_to_its_rows(self):
        view = _view(equals=(("channel", "fuel_temp_1"),))
        assert [r["channel"] for r in view["rows"]] == ["fuel_temp_1"]

    def test_several_terms_all_have_to_hold(self):
        assert _view(equals=(("unit", "degC"), ("channel", "power")))["matched"] == 0

    def test_a_row_missing_the_column_does_not_pass(self):
        """Ignoring an absent column would silently widen the filter."""
        rows = [*ROWS, {"channel": "odd", "value": 1.0}]
        view = tabulate(rows, columns=COLUMNS, title="t", equals=(("unit", "W"),))
        assert [r["channel"] for r in view["rows"]] == ["power"]

    def test_it_matches_what_the_table_SHOWS(self):
        """A reader types what is on screen, not the float behind it."""
        assert _view(equals=(("value", "950"),))["matched"] == 1


class TestSubstringSearch:
    def test_it_finds_across_the_whole_row(self):
        assert _view(search="temp")["matched"] == 2

    def test_it_matches_a_column_the_reader_was_not_thinking_of(self):
        """The point of a search over a filter: `step` is a unit, and
        somebody typing it still finds the rod."""
        assert [r["channel"] for r in _view(search="step")["rows"]] == ["rod_shim_1"]

    def test_it_ignores_case(self):
        assert _view(search="TEMP")["matched"] == 2

    def test_search_and_equals_compose(self):
        view = _view(search="temp", equals=(("unit", "degC"),))
        assert view["matched"] == 2


class TestItSaysHowHardItNarrowed:
    def test_the_footer_reports_both_numbers(self):
        """"1 shown" over six thousand rows has told the reader nothing."""
        assert "1 of 4 rows match" in _view(equals=(("channel", "power"),))["text"]

    def test_an_unfiltered_table_says_nothing_extra(self):
        assert "rows match" not in _view()["text"]

    def test_the_counts_are_carried_as_data(self):
        view = _view(search="temp")
        assert (view["matched"], view["fetched"]) == (2, 4)


class TestNarrowingHappensBeforePaging:
    def test_a_page_is_a_page_of_matches(self):
        """Filtering a page would give a page of twenty-five with three
        rows on it and call that page one of many."""
        view = _view(search="temp", page_size=1)
        assert view["returned"] == 1
        assert view["has_more"] is True

    def test_page_two_of_the_matches(self):
        view = _view(search="temp", page_size=1, page=2)
        assert [r["channel"] for r in view["rows"]] == ["fuel_temp_2"]
        assert view["has_more"] is False


class TestTheSpecSaysWhichTableThisIs:
    def test_the_filter_is_in_the_document(self):
        doc = _view(search="temp")["spec"]
        assert doc["filter"] == {"search": "temp"}

    def test_it_survives_a_round_trip(self):
        """A field the loader drops is worse than a missing field: the
        document says the table was narrowed and the spec that comes back
        says it was not."""
        spec = TableSpec(
            kind="rows", title="t",
            columns=(Column(id="channel", label="c"), Column(id="value", label="v")),
            filter=Filter(search="temp", equals=(("channel", "power"),)),
        )
        assert parse_document(spec.to_document()).filter == spec.filter

    def test_an_empty_filter_is_not_recorded(self):
        assert "filter" not in _view()["spec"]

    def test_a_filter_on_a_column_that_is_not_there_is_refused(self):
        """An empty table reads as "no such rows" rather than "no such
        column", so the mistake has to be caught where it is made."""
        with pytest.raises(SpecFormatError, match="does not have"):
            _view(equals=(("nope", "x"),))

    def test_an_unknown_filter_key_is_refused(self):
        with pytest.raises(UnknownFieldError):
            parse_document({
                "schema_version": "1.0", "kind": "rows", "title": "t",
                "columns": [{"id": "channel", "label": "c"}],
                "page": {"number": 1, "size": 25},
                "filter": {"search": "x", "regex": "y"},
            })
