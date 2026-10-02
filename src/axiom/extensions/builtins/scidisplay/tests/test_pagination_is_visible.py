# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""A full page has to be able to say there is another one.

`rows_needed` returned exactly `page * size`, so a caller fetched precisely
enough to fill the page and had nothing left over. `tabulate` then reported
`has_more: False` on EVERY full page, the "next page" line could never fire,
and a table of three hundred rows showed twenty-five and said nothing about
the rest.

Found by paging through a partner's data in the onboarding rehearsal and
noticing there was no way forward from page one.
"""

from __future__ import annotations

import pytest

from axiom.extensions.builtins.scidisplay.table_spec import (
    Page,
    rows_needed,
    tabulate,
)

COLUMNS = (("a", "a", True, "right"),)


def _rows(n):
    return [{"a": i} for i in range(n)]


def _page(rows, *, page=1, size=25):
    return tabulate(rows, columns=COLUMNS, title="t", page=page, page_size=size)


class TestTheProbeAsksForOneMoreThanItShows:
    @pytest.mark.parametrize("page,size", [(1, 25), (2, 25), (1, 10), (4, 50)])
    def test_one_more_than_the_page_reaches(self, page, size):
        assert rows_needed(Page(number=page, size=size), sorted_=False) == page * size + 1

    def test_a_sorted_page_still_takes_its_window(self):
        """The extra row must not shrink the sort window, which is the
        expensive case and the one a caller opted into."""
        assert rows_needed(Page(number=1, size=25), sorted_=True, sort_window=10_000) == 10_000


class TestHasMoreIsAnswerable:
    def test_a_full_page_with_more_behind_it_says_so(self):
        """The defect, directly: this reported False before."""
        fetched = rows_needed(Page(number=1, size=25), sorted_=False)
        out = _page(_rows(fetched))
        assert out["returned"] == 25
        assert out["has_more"] is True

    def test_the_extra_row_is_not_shown(self):
        """It is a probe, not content. Showing 26 rows on a 25-row page
        would trade one bug for a worse one."""
        out = _page(_rows(rows_needed(Page(number=1, size=25), sorted_=False)))
        assert out["returned"] == 25
        assert [r["a"] for r in out["rows"]] == list(range(25))

    def test_a_genuinely_final_page_says_there_is_no_more(self):
        assert _page(_rows(25))["has_more"] is False

    def test_a_short_page_says_there_is_no_more(self):
        assert _page(_rows(7))["has_more"] is False

    def test_an_empty_result_says_there_is_no_more(self):
        assert _page([])["has_more"] is False


class TestItHoldsOnLaterPages:
    def test_page_two_of_three_says_there_is_more(self):
        fetched = rows_needed(Page(number=2, size=25), sorted_=False)
        out = _page(_rows(fetched), page=2)
        assert out["returned"] == 25
        assert out["has_more"] is True

    def test_the_last_page_does_not(self):
        out = _page(_rows(50), page=2)
        assert out["returned"] == 25
        assert out["has_more"] is False
