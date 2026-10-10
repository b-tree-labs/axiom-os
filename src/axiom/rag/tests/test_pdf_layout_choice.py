# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Choosing between visual layout and reading order, per document.

``pdftotext -layout`` reproduces a page as it looks, which keeps a table's
columns aligned. On a **two-column** document it does the same thing and the
result is unreadable: it reads straight across the gutter, splicing a sentence
from the left column into an unrelated one from the right.

Measured on a real two-column reactor paper before this: 61% of non-empty lines
spliced, 184 of them ending mid-word. That is not merely noisy. It is
*plausible* — it reads as prose, so a model answers from it confidently and the
answer is a splice of two unrelated sentences. Most technical papers and many
manuals are two-column, so this was the common case rather than the corner.
"""

from __future__ import annotations

from axiom.rag.extract import _MULTICOLUMN_SHARE, _spliced_share

# Lines at a real page width. The first version of this fixture used ~85
# characters and failed once the detector learned to require length — a
# fixture narrower than the page it stands for was testing nothing.
TWO_COLUMN = """\
1. Introduction                                                                  University of Missouri is expected to develop
The AGN-201 is a low power reactor used                                          a new reactor to continue this mission. TRIGA
for teaching and research at Idaho State                                         reactors at universities provide irradiation
University. The reactor has operated                                             facilities for geochronology, radiation hard
since 1965 and remains in service today.                                         ness testing, and radiotracer production for
"""

SINGLE_COLUMN = """\
1. Introduction

The AGN-201 is a low power reactor used for teaching and research at Idaho
State University. The reactor has operated since 1965 and remains in service.
"""

A_TABLE = """\
Parameter            Value      Units
Thermal power        5.0        W
Core height          38.1       cm
Fuel enrichment      19.75      wt%
"""


class TestSplicedShare:
    def test_two_columns_read_across_score_high(self):
        assert _spliced_share(TWO_COLUMN) > _MULTICOLUMN_SHARE

    def test_ordinary_prose_scores_zero(self):
        assert _spliced_share(SINGLE_COLUMN) == 0.0

    def test_a_table_is_not_mistaken_for_two_columns(self):
        """The discrimination that matters.

        A table has wide runs of spaces too. If it scored as multi-column, the
        fix would throw away the alignment that makes a table readable — which
        is the one thing ``-layout`` is for.
        """
        assert _spliced_share(A_TABLE) <= _MULTICOLUMN_SHARE

    def test_empty_text_is_not_a_division_by_zero(self):
        assert _spliced_share("") == 0.0
        assert _spliced_share("\n\n\n") == 0.0


class TestTheChoice:
    def _extract(self, monkeypatch, laid_out, reading_order):
        from axiom.rag import extract

        calls = []

        def fake(path, *, layout):
            calls.append(layout)
            return laid_out if layout else reading_order

        monkeypatch.setattr(extract, "_pdftotext", fake)
        from pathlib import Path

        return extract._extract_pdf_native(Path("paper.pdf")), calls

    def test_a_multi_column_document_is_re_extracted_in_reading_order(
        self, monkeypatch
    ):
        text, calls = self._extract(monkeypatch, TWO_COLUMN, SINGLE_COLUMN)

        assert text == SINGLE_COLUMN
        assert calls == [True, False], "layout first, then reading order"

    def test_a_single_column_document_keeps_the_layout_pass(self, monkeypatch):
        """One extraction, not two: the layout is what preserves its tables."""
        text, calls = self._extract(monkeypatch, A_TABLE, SINGLE_COLUMN)

        assert text == A_TABLE
        assert calls == [True], "no second pass when the first is fine"

    def test_it_falls_back_to_the_layout_pass_rather_than_returning_nothing(
        self, monkeypatch
    ):
        """A spliced extraction is bad. No extraction is worse."""
        text, _ = self._extract(monkeypatch, TWO_COLUMN, None)

        assert text == TWO_COLUMN

    def test_no_native_text_at_all_returns_none(self, monkeypatch):
        """So the caller can still route a scanned page through OCR."""
        from axiom.rag import extract

        monkeypatch.setattr(extract, "_pdftotext", lambda path, *, layout: None)
        monkeypatch.setattr(extract, "PdfReader", None, raising=False)
        from pathlib import Path

        # pypdf may or may not be installed; either way this must not raise.
        assert extract._extract_pdf_native(Path("scan.pdf")) in (None, "")


TABLE_IN_TWO_COLUMNS = """\
Table 2                                                                          illustrate how the model
Modeled and operational quantities.                                              shows a three-feature set
                          Modeled Value    Experiment       Difference           behind the ability of IF
  Coarse Control Rod ($)  1.86 (+/-0.07)   1.68 (+/-0.06)   -0.18 (0.09)         each tree isolates a poi
  keff                    1.00165          1.00000          165 pcm              randomly selecting a fea
The reactor has operated                                                         paths in these trees are
since 1965 and remains in service today at Idaho State University for teaching   and for research besides
"""


class TestTablesSurviveTheSwitchToReadingOrder:
    """Reading order fixes two-column prose and costs multi-column tables.

    It emits a table's row labels as a flat list and its values as another, so
    "what is the modeled value for X" stops being answerable — or worse, is
    answered from an unlabelled sequence. Measured on a real two-column paper:
    the layout pass held ``keff  1.00165  1.00000  165 pcm`` with its columns
    intact; reading order held ``keff`` and ``1.00165`` in different places
    with nothing joining them.
    """

    def test_table_rows_are_recovered_from_the_layout_pass(self):
        from axiom.rag.extract import _recovered_tables

        recovered = _recovered_tables(TABLE_IN_TWO_COLUMNS)

        assert "keff" in recovered
        assert "1.00165" in recovered

    def test_a_value_stays_joined_to_the_parameter_it_belongs_to(self):
        """The whole point. A severed value is worse than a missing one."""
        from axiom.rag.extract import _recovered_tables

        row = next(
            line
            for line in _recovered_tables(TABLE_IN_TWO_COLUMNS).splitlines()
            if "keff" in line
        )

        assert "1.00165" in row and "1.00000" in row

    def test_spliced_prose_is_not_mistaken_for_a_table(self):
        """A table row has a gap between each of ITS columns, so three or more.
        Two page-columns read across have exactly one, between them."""
        from axiom.rag.extract import _recovered_tables

        recovered = _recovered_tables(TABLE_IN_TWO_COLUMNS)

        assert "since 1965" not in recovered

    def test_nothing_is_appended_when_there_is_no_table(self):
        from axiom.rag.extract import _recovered_tables

        assert _recovered_tables(TWO_COLUMN) == ""

    def test_the_recovered_block_is_labelled(self):
        """A reader meeting a row with a fragment of another column stuck to
        its end should be able to tell where it came from."""
        from axiom.rag.extract import _recovered_tables

        assert "recovered from the page layout" in _recovered_tables(
            TABLE_IN_TWO_COLUMNS
        )

    def test_the_prose_is_still_the_reading_order_version(self, monkeypatch):
        from pathlib import Path

        from axiom.rag import extract

        monkeypatch.setattr(
            extract,
            "_pdftotext",
            lambda path, *, layout: TABLE_IN_TWO_COLUMNS if layout else SINGLE_COLUMN,
        )

        text = extract._extract_pdf_native(Path("paper.pdf"))

        assert text.startswith(SINGLE_COLUMN)
        assert "keff" in text
