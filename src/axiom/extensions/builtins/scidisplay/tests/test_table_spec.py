# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""The table spec: one document, four surfaces.

``chart_spec`` settled how a picture is described. A table needs the same
treatment and for the same reason: the alternative is four implementations
of "show me these rows" — one in the CLI, one in the agent's answer, one
behind MCP, one in the web app — which drift, and three of which cannot be
shared or cited.

So a table is a document too: which columns, what sort, which page, how
large. Inert, versioned, diffable, speakable.

**The shape is not invented here.** The web surface already exists — the
``shared/data-table`` module, whose ``DataTable`` takes ``columns`` of
``{id, label, sortable, align, render}`` plus ``sortColumn`` /
``sortDirection``, and whose ``usePaginatedRemoteData`` fetches
``{items, total, hasMore}`` a page at a time. This spec is deliberately the
same vocabulary, so the existing component renders it without a translation
layer, and so a CLI table and a web table cannot disagree about what sorted
means.

**Paging is in the spec, not bolted beside it.** That is the scalable
claim: a spec names a page of a result, so no surface ever holds a ten
million row table in order to show twenty-five of them.
"""

from __future__ import annotations

import pytest

from axiom.extensions.builtins.scidisplay import table_spec


def _cols():
    return [
        table_spec.Column(id="ts", label="Time", sortable=True),
        table_spec.Column(id="value", label="Value", align="right", sortable=True),
        table_spec.Column(id="unit", label="Unit"),
    ]


class TestItIsADocument:
    def test_it_states_its_schema_version(self):
        spec = table_spec.TableSpec(kind="rows", title="t", columns=_cols())
        assert spec.schema_version == table_spec.SCHEMA_VERSION

    def test_a_document_round_trips(self):
        spec = table_spec.TableSpec(kind="rows", title="t", columns=_cols())
        assert table_spec.parse_document(spec.to_document()) == spec

    def test_a_missing_schema_version_is_refused_not_defaulted(self):
        """A reader that guesses cannot know which fields a document carries."""
        doc = table_spec.TableSpec(kind="rows", title="t", columns=_cols()).to_document()
        doc.pop("schema_version")
        with pytest.raises(table_spec.SchemaVersionError):
            table_spec.parse_document(doc)

    def test_an_unknown_key_is_refused_rather_than_dropped(self):
        """A spec that loses content on the way through is not diffable."""
        doc = table_spec.TableSpec(kind="rows", title="t", columns=_cols()).to_document()
        doc["colour"] = "red"
        with pytest.raises(table_spec.UnknownFieldError, match="colour"):
            table_spec.parse_document(doc)

    def test_an_unregistered_kind_names_what_is_registered(self):
        with pytest.raises(table_spec.UnregisteredKindError, match="rows"):
            table_spec.TableSpec(kind="carousel", title="t", columns=_cols())


class TestColumns:
    def test_a_column_needs_an_id_and_a_label(self):
        with pytest.raises(table_spec.SpecFormatError, match="id"):
            table_spec.Column(id="", label="Time")

    def test_alignment_is_a_closed_set(self):
        """It maps onto a CSS class, not free text."""
        with pytest.raises(table_spec.SpecFormatError, match="align"):
            table_spec.Column(id="v", label="V", align="middle")

    def test_alignment_defaults_to_left(self):
        assert table_spec.Column(id="v", label="V").align == "left"

    def test_duplicate_column_ids_are_refused(self):
        """rowKey and sortColumn both address a column by id; two columns
        answering to one id makes both ambiguous."""
        with pytest.raises(table_spec.SpecFormatError, match="duplicate"):
            table_spec.TableSpec(
                kind="rows", title="t",
                columns=[table_spec.Column(id="a", label="A"),
                         table_spec.Column(id="a", label="B")],
            )

    def test_a_table_with_no_columns_is_refused(self):
        with pytest.raises(table_spec.SpecFormatError, match="column"):
            table_spec.TableSpec(kind="rows", title="t", columns=[])


class TestSortIsAddressedByColumnId:
    def test_sorting_by_a_declared_column_is_fine(self):
        spec = table_spec.TableSpec(
            kind="rows", title="t", columns=_cols(),
            sort=table_spec.Sort(column="ts", direction="desc"),
        )
        assert spec.sort.column == "ts"

    def test_sorting_by_a_column_that_is_not_there_is_refused(self):
        """It would render as unsorted, which looks like it worked."""
        with pytest.raises(table_spec.SpecFormatError, match="nope"):
            table_spec.TableSpec(
                kind="rows", title="t", columns=_cols(),
                sort=table_spec.Sort(column="nope", direction="asc"),
            )

    def test_sorting_by_a_column_that_is_not_sortable_is_refused(self):
        with pytest.raises(table_spec.SpecFormatError, match="sortable"):
            table_spec.TableSpec(
                kind="rows", title="t", columns=_cols(),
                sort=table_spec.Sort(column="unit", direction="asc"),
            )

    def test_direction_is_asc_or_desc(self):
        with pytest.raises(table_spec.SpecFormatError, match="direction"):
            table_spec.Sort(column="ts", direction="sideways")


class TestPagingIsInTheSpec:
    """The scalable claim: a spec names a PAGE, so no surface ever holds ten
    million rows in order to show twenty-five."""

    def test_a_default_page_matches_the_web_component(self):
        page = table_spec.Page()
        assert page.size == table_spec.PAGE_SIZE_DEFAULT == 25
        assert page.number == 1

    def test_page_numbers_start_at_one_not_zero(self):
        """Because the existing fetchPage(1) does."""
        with pytest.raises(table_spec.SpecFormatError, match="page"):
            table_spec.Page(number=0)

    def test_an_unbounded_page_size_is_refused(self):
        with pytest.raises(table_spec.SpecFormatError, match="size"):
            table_spec.Page(size=1_000_000)

    def test_the_cap_is_stated_rather_than_implied(self):
        assert table_spec.PAGE_SIZE_MAX >= table_spec.PAGE_SIZE_DEFAULT


class TestItRendersOnTheSurfaceThatAlreadyExists:
    """The props are the ones `shared/data-table` already takes, so the
    existing component renders a spec with no translation layer."""

    def test_it_emits_the_datatable_column_shape(self):
        spec = table_spec.TableSpec(kind="rows", title="t", columns=_cols())
        props = spec.to_datatable_props()
        assert props["columns"][0] == {
            "id": "ts", "label": "Time", "sortable": True, "align": "left"
        }

    def test_it_emits_the_sort_props_by_the_names_the_component_uses(self):
        spec = table_spec.TableSpec(
            kind="rows", title="t", columns=_cols(),
            sort=table_spec.Sort(column="value", direction="desc"),
        )
        props = spec.to_datatable_props()
        assert props["sortColumn"] == "value"
        assert props["sortDirection"] == "desc"

    def test_an_unsorted_table_says_so_rather_than_inventing_a_column(self):
        props = table_spec.TableSpec(
            kind="rows", title="t", columns=_cols()
        ).to_datatable_props()
        assert props["sortColumn"] is None

    def test_it_emits_the_page_size_the_hook_expects(self):
        spec = table_spec.TableSpec(kind="rows", title="t", columns=_cols())
        assert spec.to_datatable_props()["pageSize"] == 25


class TestItNamesNoDomain:
    def test_the_module_carries_no_domain_noun(self):
        """Axiom is the domain-agnostic platform; a domain lives entirely in
        the values a spec carries. Same rule chart_spec holds itself to."""
        import pathlib

        source = pathlib.Path(table_spec.__file__).read_text(encoding="utf-8").lower()
        for noun in ("reactor", "nuclear", "soil", "telemetry", "facility"):
            assert noun not in source, f"table_spec names {noun!r}"

    def test_it_imports_nothing_that_touches_a_store(self):
        """Inert by design, exactly as the chart spec is."""
        import pathlib

        source = pathlib.Path(table_spec.__file__).read_text(encoding="utf-8")
        for forbidden in ("sqlalchemy", "psycopg", "session_for", "requests", "httpx"):
            assert forbidden not in source


class TestTheSurfacesCannotDisagree:
    """The DRY claim, as tests rather than as an intention.

    Four surfaces render this document: a terminal verb, an agent's answer,
    an MCP result, and the web component. The failure mode being guarded
    against is the ordinary one — each grows its own idea of column order,
    or of what a sort arrow means — which nobody notices until two people
    compare a screenshot with a paste.
    """

    def _spec(self):
        return table_spec.TableSpec(
            kind="rows", title="Readings", columns=_cols(),
            sort=table_spec.Sort(column="value", direction="desc"),
        )

    def _rows(self):
        return [
            {"ts": "2026-09-21T10:00:00Z", "value": 51.2, "unit": "degC"},
            {"ts": "2026-09-21T10:00:01Z", "value": 12.4, "unit": None},
        ]

    def test_both_surfaces_take_their_columns_from_the_same_place(self):
        spec = self._spec()
        web = [c["id"] for c in spec.to_datatable_props()["columns"]]
        text = table_spec.render_text(spec, self._rows())
        header = text.splitlines()[2]
        assert web == [c.id for c in spec.columns]
        for column in spec.columns:
            assert column.label.upper() in header
        # ...and in the same order.
        positions = [header.index(c.label.upper()) for c in spec.columns]
        assert positions == sorted(positions)

    def test_both_surfaces_mark_the_same_sorted_column(self):
        spec = self._spec()
        assert spec.to_datatable_props()["sortColumn"] == "value"
        header = table_spec.render_text(spec, self._rows()).splitlines()[2]
        assert "VALUE ↓" in header, header

    def test_the_arrow_matches_the_direction_on_both(self):
        spec = table_spec.TableSpec(
            kind="rows", title="t", columns=_cols(),
            sort=table_spec.Sort(column="ts", direction="asc"),
        )
        assert spec.to_datatable_props()["sortDirection"] == "asc"
        assert "↑" in table_spec.render_text(spec, self._rows())

    def test_a_right_aligned_column_is_right_aligned_in_text_too(self):
        text = table_spec.render_text(self._spec(), self._rows())
        body = [ln for ln in text.splitlines() if "51.2" in ln][0]
        assert body.index("51.2") > body.index("2026-09-21T10:00:00Z")

    def test_an_absent_value_is_a_dash_not_an_empty_cell(self):
        """Empty reads as "nothing here"; a dash reads as "we looked"."""
        rows = self._rows() + [{"ts": "2026-09-21T10:00:02Z", "value": None, "unit": "degC"}]
        text = table_spec.render_text(self._spec(), rows)
        assert "—" in text

    def test_an_absent_UNIT_says_so_in_words_rather_than_a_dash(self):
        """This test used to be satisfied by the dash in the UNIT column,
        and that was the weaker claim.

        A dash in a VALUE column is unambiguous: no reading. A dash in a
        UNIT column is not — it reads equally as "dimensionless" and as
        "nobody declared one", and those are different statements about the
        number beside it.

        It also never fired on real data. `format_number` turns ``None``
        into a dash and leaves ``''`` alone, and what a producer actually
        sends is ``''`` — so the column rendered blank for the rows this
        was meant to protect.
        """
        rows = [{"ts": "2026-09-21T10:00:00Z", "value": 12.4, "unit": ""}]
        text = table_spec.render_text(self._spec(), rows)
        assert "unit not declared" in text
        assert "12.4" in text

    def test_the_text_surface_says_which_page_it_drew(self):
        text = table_spec.render_text(self._spec(), self._rows())
        # Reworded when the layout moved to cli_format: "2 of at most 25"
        # read as though 25 were a ceiling on what exists rather than the
        # page size.
        assert "page 1" in text and "2 shown, 25 per page" in text

    def test_rendering_no_rows_says_so_rather_than_drawing_nothing(self):
        text = table_spec.render_text(self._spec(), [])
        assert "(no rows)" in text


class TestTheColumnsLineUp:
    """Seen in a live run: the sorted column's heading is wider than its
    label because of the arrow, and measuring the bare label left every
    column after it shifted by two.

    Alignment is not decoration in a table — a misaligned column is read as
    belonging to its neighbour.
    """

    def _spec(self, direction="desc"):
        return table_spec.TableSpec(
            kind="rows", title="t", columns=_cols(),
            sort=table_spec.Sort(column="value", direction=direction),
        )

    def _grid(self, spec, rows):
        return [
            ln for ln in table_spec.render_text(spec, rows).splitlines()
            if ln and not ln.startswith("t") and not ln.startswith("page")
        ]

    def test_the_rule_is_as_wide_as_the_heading_above_it(self):
        rows = [{"ts": "2026-09-21T10:00:00Z", "value": 1.0, "unit": "degC"}]
        header, rule, *_ = self._grid(self._spec(), rows)
        for head_cell, rule_cell in zip(header.split("  "), rule.split("  "), strict=False):
            if head_cell and rule_cell:
                assert len(rule_cell) >= len(head_cell.rstrip()), (
                    f"heading {head_cell!r} is wider than its rule {rule_cell!r}"
                )

    def test_a_marked_column_does_not_shift_the_ones_after_it(self):
        rows = [{"ts": "2026-09-21T10:00:00Z", "value": 1.0, "unit": "degC"}]
        unsorted = table_spec.TableSpec(kind="rows", title="t", columns=_cols())
        marked_cols = len(self._grid(self._spec(), rows)[0].split("  "))
        plain_cols = len(self._grid(unsorted, rows)[0].split("  "))
        assert marked_cols == plain_cols

    def test_every_body_row_has_the_same_column_starts(self):
        rows = [
            {"ts": "2026-09-21T10:00:00Z", "value": 1.0, "unit": "degC"},
            {"ts": "2026-09-21T10:00:01Z", "value": 1234.5, "unit": "gal/min"},
        ]
        lines = self._grid(self._spec(), rows)
        rule = lines[1]
        # Column starts are where the rule's dash runs begin.
        starts = [i for i, ch in enumerate(rule) if ch == "-" and (i == 0 or rule[i - 1] == " ")]
        for body in lines[2:]:
            for start in starts[:-1]:
                assert start < len(body), f"row is shorter than the rule: {body!r}"


class TestAnyExtensionCanHaveATableWithoutWritingOne:
    """The layering, as a test.

    The first cut put paging, sorting, slicing and rendering inside one
    extension's skill, leaving only the rows and the column names genuinely
    its own. Any second extension wanting a table would have copied all of
    it — which is the duplication the spec was written to prevent, rebuilt
    one layer up.

    So the mechanics live here and an extension supplies what is actually
    its: its rows, and what its columns are called.
    """

    #: Deliberately nothing to do with signals — if the generic path only
    #: fits the extension it was extracted from, it is not generic.
    _INVOICES = [
        {"id": "INV-3", "customer": "Fangio", "amount": 240.0, "status": "paid"},
        {"id": "INV-1", "customer": "Ascari", "amount": 1300.5, "status": "open"},
        {"id": "INV-2", "customer": "Moss", "amount": 75.25, "status": "open"},
    ]
    _COLUMNS = (
        ("id", "Invoice", True, "left"),
        ("customer", "Customer", True, "left"),
        ("amount", "Amount", True, "right"),
        ("status", "Status", False, "left"),
    )

    def test_an_extension_declares_columns_in_the_shorthand(self):
        cols = table_spec.columns_from(self._COLUMNS)
        assert [c.id for c in cols] == ["id", "customer", "amount", "status"]
        assert cols[2].align == "right" and cols[3].sortable is False

    def test_columns_may_also_be_mappings_or_objects(self):
        mixed = [
            table_spec.Column(id="a", label="A"),
            {"id": "b", "label": "B", "sortable": True},
            ("c", "C"),
        ]
        assert [c.id for c in table_spec.columns_from(mixed)] == ["a", "b", "c"]

    def test_tabulate_gives_every_surface_from_rows_and_columns_alone(self):
        out = table_spec.tabulate(
            self._INVOICES, columns=self._COLUMNS, title="Open invoices"
        )
        assert set(out) >= {"spec", "rows", "text", "datatable_props", "has_more"}
        assert "Open invoices" in out["text"]
        assert out["datatable_props"]["pageSize"] == 25

    def test_it_sorts_by_a_declared_column(self):
        out = table_spec.tabulate(
            self._INVOICES, columns=self._COLUMNS, title="t",
            sort="amount", direction="desc",
        )
        assert [r["amount"] for r in out["rows"]] == [1300.5, 240.0, 75.25]

    def test_it_pages(self):
        out = table_spec.tabulate(
            self._INVOICES, columns=self._COLUMNS, title="t",
            page=2, page_size=2, sort="id",
        )
        assert out["returned"] == 1
        assert out["has_more"] is False

    def test_has_more_is_true_while_there_is_more(self):
        out = table_spec.tabulate(
            self._INVOICES, columns=self._COLUMNS, title="t", page=1, page_size=2
        )
        assert out["has_more"] is True

    def test_a_bad_sort_raises_the_spec_error_for_the_caller_to_pass_on(self):
        with pytest.raises(table_spec.SpecFormatError, match="status"):
            table_spec.tabulate(
                self._INVOICES, columns=self._COLUMNS, title="t", sort="status"
            )


class TestSortingDoesNotFallOverOnRealData:
    def test_a_column_of_mixed_types_does_not_raise(self):
        """A column can hold a reading and the reason one is missing. A
        traceback from asking for a sorted page is a worse answer than an
        odd order."""
        rows = [{"v": 3.0}, {"v": "n/a"}, {"v": 1.0}, {"v": None}]
        out = table_spec.tabulate(
            rows, columns=(("v", "V", True, "right"),), title="t", sort="v"
        )
        assert [r["v"] for r in out["rows"]] == [1.0, 3.0, "n/a", None]

    def test_absent_values_sort_last_in_both_directions(self):
        rows = [{"v": 2.0}, {"v": None}, {"v": 1.0}]
        for direction in ("asc", "desc"):
            out = table_spec.tabulate(
                rows, columns=(("v", "V", True, "right"),), title="t",
                sort="v", direction=direction,
            )
            assert out["rows"][-1]["v"] is None, (
                f"a missing value sorted to the front on {direction}; it is "
                "not a small value"
            )


class TestTheFetchSizeIsGenericToo:
    def test_an_unsorted_page_needs_what_it_reaches_plus_the_probe(self):
        """Updated 2026-09-23. This asserted exactly 75, which is the
        defect: fetching exactly what the page shows leaves nothing over,
        so `has_more` is False on every full page and the next-page line
        can never fire. The one extra row IS has_more."""
        page = table_spec.Page(number=3, size=25)
        assert table_spec.rows_needed(page, sorted_=False) == 76

    def test_a_sorted_page_needs_the_window_it_sorts_over(self):
        page = table_spec.Page(number=1, size=25)
        assert table_spec.rows_needed(page, sorted_=True) > 25

    def test_a_deep_unsorted_page_still_wins_over_the_sort_window(self):
        page = table_spec.Page(number=1000, size=25)
        assert table_spec.rows_needed(page, sorted_=True) == 25_001


class TestASurfaceMaySuppressTheTitle:
    """The spec always carries a title — a document that may be shared or
    cited needs one. A surface that already has the context may not want to
    print it, and callers parse the first line as the header.
    """

    def _spec(self):
        return table_spec.TableSpec(kind="rows", title="Readings", columns=_cols())

    def test_the_title_is_shown_by_default(self):
        assert table_spec.render_text(self._spec(), []).splitlines()[0] == "Readings"

    def test_suppressed_the_header_is_the_first_line(self):
        first = table_spec.render_text(
            self._spec(), [], show_title=False
        ).splitlines()[0]
        assert "TIME" in first and "Readings" not in first

    def test_the_spec_still_carries_the_title_either_way(self):
        """Suppressing it is a rendering choice, not a change to the
        document — the shared copy must still say what it is."""
        spec = self._spec()
        table_spec.render_text(spec, [], show_title=False)
        assert spec.title == "Readings"
        assert spec.to_document()["title"] == "Readings"

    def test_tabulate_passes_it_through(self):
        out = table_spec.tabulate(
            [{"ts": "x", "value": 1.0, "unit": "u"}],
            columns=_cols(), title="Readings", show_title=False,
        )
        assert not out["text"].startswith("Readings")


class TestASurfaceMaySuppressTheFooter:
    """The footer says which page this is. That is the point in a paged
    view and noise in a listing that shows everything it has — a verb
    printing all of something should not say "page 1 of 1"."""

    def _rows(self):
        return [{"ts": "2026-09-21T10:00:00Z", "value": 1.0, "unit": "degC"}]

    def test_shown_by_default(self):
        out = table_spec.tabulate(self._rows(), columns=_cols(), title="t")
        assert "page 1" in out["text"]

    def test_suppressed_the_last_line_is_a_row(self):
        out = table_spec.tabulate(
            self._rows(), columns=_cols(), title="t",
            show_title=False, show_footer=False,
        )
        assert "page" not in out["text"]
        assert out["text"].splitlines()[-1].strip().startswith("2026-09-21")

    def test_the_page_is_still_in_the_document(self):
        """Suppressing it is a rendering choice; a shared copy still says
        which page it is."""
        out = table_spec.tabulate(
            self._rows(), columns=_cols(), title="t", show_footer=False
        )
        assert out["spec"]["page"]["number"] == 1
        assert out["page"] == 1

    def test_no_trailing_blank_line_when_suppressed(self):
        out = table_spec.tabulate(
            self._rows(), columns=_cols(), title="t",
            show_title=False, show_footer=False,
        )
        assert out["text"] == out["text"].rstrip()


class TestADataCellIsNeverAltered:
    """cli_format elides what will not fit, which is right for prose and
    wrong here. An elided timestamp cannot be used and "pred…ted" is not a
    source class. Overflowing a narrow terminal is the lesser harm: an ugly
    wrap is visible, a truncated value reads as the value.
    """

    _WIDE = [
        {"ts": "2026-09-21T10:00:00.000Z", "value": 1.0,
         "unit": "rom_predicted_outlet_temperature_celsius"},
    ]

    def test_nothing_is_elided_even_when_the_terminal_is_narrow(self, monkeypatch):
        monkeypatch.setattr(
            "axiom.infra.cli_format.terminal_width", lambda reserve=0: 40
        )
        text = table_spec.render_text(
            table_spec.TableSpec(kind="rows", title="t", columns=_cols()), self._WIDE
        )
        assert "…" not in text, text
        assert "rom_predicted_outlet_temperature_celsius" in text
        assert "2026-09-21T10:00:00.000Z" in text

    def test_it_uses_the_terminal_when_the_table_does_fit(self, monkeypatch):
        """Not always maximal — a table that fits should sit in the width it
        was given rather than sprawling."""
        monkeypatch.setattr(
            "axiom.infra.cli_format.terminal_width", lambda reserve=0: 200
        )
        text = table_spec.render_text(
            table_spec.TableSpec(kind="rows", title="t", columns=_cols()),
            [{"ts": "a", "value": 1.0, "unit": "b"}],
        )
        assert "…" not in text


class TestTheLayoutIsNotWrittenTwice:
    def test_rendering_goes_through_cli_format(self):
        """A second table renderer is how two verbs end up indenting
        differently — which is what happened before this delegated."""
        import inspect

        source = inspect.getsource(table_spec.render_text)
        assert "cli_format" in source

    def test_the_standard_indent_is_not_reinvented(self):
        """cli_format owns the indent; hard-coding one here would drift."""
        import inspect

        source = inspect.getsource(table_spec.render_text)
        assert '"  " +' not in source
