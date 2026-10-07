# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""A sorted page over a window must say that is what it is.

`rows_needed(page, sorted_=True)` fetches a 10,000-row window, which is the
right trade — sorting a whole stream to show twenty-five rows is not a
table, it is a query. But the top row of a "sort by value, descending" then
reads as the stream's maximum and is only the window's, and the two render
identically.

This is the one case where a table can be confidently wrong, so the caller
that chose the window says so and the footer carries it.
"""

from __future__ import annotations

from axiom.extensions.builtins.scidisplay.table_spec import tabulate

COLUMNS = (("channel", "channel", True, "left"), ("value", "value", True, "right"))
ROWS = [{"channel": "a", "value": 3.0}, {"channel": "b", "value": 1.0},
        {"channel": "c", "value": 2.0}]


class TestACompleteTableSaysNothingExtra:
    def test_the_footer_is_unchanged(self):
        text = tabulate(ROWS, columns=COLUMNS, title="t", sort="value")["text"]
        assert "sorted over" not in text

    def test_completeness_is_reported_as_data(self):
        assert tabulate(ROWS, columns=COLUMNS, title="t")["complete"] is True


class TestAWindowedSortIsDeclared:
    def test_the_footer_says_the_sort_saw_a_window(self):
        text = tabulate(ROWS, columns=COLUMNS, title="t", sort="value",
                        direction="desc", complete=False)["text"]
        assert "sorted over the rows fetched" in text

    def test_it_names_the_direction_in_the_readers_terms(self):
        """"The highest value here may not be the stream's" is the claim a
        reader was about to make. Saying "descending" would not be."""
        text = tabulate(ROWS, columns=COLUMNS, title="t", sort="value",
                        direction="desc", complete=False)["text"]
        assert "highest" in text
        text = tabulate(ROWS, columns=COLUMNS, title="t", sort="value",
                        direction="asc", complete=False)["text"]
        assert "lowest" in text

    def test_an_unsorted_window_says_the_simpler_thing(self):
        """Without a sort there is no false extreme to correct — only more
        rows than were fetched."""
        text = tabulate(ROWS, columns=COLUMNS, title="t", complete=False)["text"]
        assert "more rows exist" in text
        assert "sorted over" not in text

    def test_an_incomplete_fetch_always_has_more(self):
        """Every row fitted on the page, so `has_more` would have said
        False — while rows the fetch never reached were waiting."""
        view = tabulate(ROWS, columns=COLUMNS, title="t", complete=False)
        assert view["has_more"] is True

    def test_the_caveat_is_absent_when_the_footer_is(self):
        """A caller that suppresses the footer has its own surround; the
        caveat must not appear on its own in the middle of one."""
        text = tabulate(ROWS, columns=COLUMNS, title="t", sort="value",
                        complete=False, show_footer=False)["text"]
        assert "sorted over" not in text
