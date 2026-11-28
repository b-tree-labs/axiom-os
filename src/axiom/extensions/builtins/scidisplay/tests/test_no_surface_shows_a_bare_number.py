# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Every surface, given a reading with no declared unit, must say so.

Behavioural, per surface, because the defect was never in one place. Each
renderer wrote its own `if unit:` and each one independently decided that
absent meant print nothing. A test that only checked the units helper would
have passed against every one of them.

The fixture is a live install's own data: `Power` running to 1,170,000 with
no unit declared, which is what 19.1 million served rows actually looked like.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from ..chart_render import render_comparison, render_timeseries
from ..chart_svg import Series, render_svg
from ..units import UNDECLARED, UNDECLARED_BRIEF

#: The real shape: a big number, a name that does not imply a unit, nothing
#: in the unit column.
UNITLESS_ROWS = [
    {"ts": "2026-09-01T00:00:00Z", "channel": "Power", "value": 0.0, "unit": ""},
    {"ts": "2026-09-01T01:00:00Z", "channel": "Power", "value": 585000.0, "unit": ""},
    {"ts": "2026-09-01T02:00:00Z", "channel": "Power", "value": 1170000.0, "unit": ""},
]

DECLARED_ROWS = [dict(r, unit="W") for r in UNITLESS_ROWS]


def _says_absent(text: str) -> bool:
    return UNDECLARED in text or UNDECLARED_BRIEF in text


class TestTheTerminalTimeseries:
    def _render(self, rows):
        return "\n".join(
            render_timeseries(
                rows, time_column="ts", value_column="value", series_column="channel"
            )
        )

    def test_an_undeclared_unit_is_stated(self):
        assert _says_absent(self._render(UNITLESS_ROWS))

    def test_the_number_is_not_left_to_stand_alone(self):
        """`0 … 1170000` reads as a count. It is watts."""
        out = self._render(UNITLESS_ROWS)
        assert "1,170,000" in out or "1170000" in out
        assert _says_absent(out)

    def test_a_declared_unit_is_not_disfigured(self):
        """The fix must not make the normal case worse, or it gets reverted."""
        out = self._render(DECLARED_ROWS)
        assert "W" in out
        assert not _says_absent(out)


class TestTheTerminalComparison:
    def _render(self, rows):
        return "\n".join(
            render_comparison(
                rows, time_column="ts", value_column="value", series_column="channel"
            )
        )

    def test_an_undeclared_unit_is_stated(self):
        assert _says_absent(self._render(UNITLESS_ROWS))

    def test_the_worst_gap_carries_it_too(self):
        """A gap is a measurement like any other, and it was printed bare by
        its own copy of the same `if unit:`."""
        rows = UNITLESS_ROWS + [
            dict(r, source_class="modelled", value=r["value"] * 1.1)
            for r in UNITLESS_ROWS
        ]
        out = "\n".join(
            render_comparison(
                rows,
                time_column="ts",
                value_column="value",
                series_column="channel",
                class_column="source_class",
            )
        )
        if "worst gap" in out:
            gap_line = next(ln for ln in out.splitlines() if "worst gap" in ln)
            assert _says_absent(gap_line)


class TestTheFigure:
    POINTS = [
        (datetime.fromisoformat(r["ts"].replace("Z", "+00:00")).astimezone(UTC), r["value"])
        for r in UNITLESS_ROWS
    ]

    def _svg(self, unit):
        return render_svg(
            [Series("Power", self.POINTS, unit=unit)],
            title="site-a — power",
            subtitle="",
            provenance="test",
        )

    def test_the_axis_says_the_unit_is_not_declared(self):
        """This was `if axis_unit:` — no unit meant NO AXIS LABEL, and an
        unlabelled axis is not a missing caption, it is a claim that the
        numbers need no unit."""
        assert _says_absent(self._svg(""))

    def test_a_declared_unit_still_labels_the_axis(self):
        svg = self._svg("W")
        assert not _says_absent(svg)
        assert ">W<" in svg or "W</text>" in svg

    def test_the_series_reading_is_not_bare(self):
        """The end-of-line label had its own `' ' + own_unit if own_unit`."""
        assert _says_absent(self._svg(""))

    def test_it_is_still_valid_svg(self):
        svg = self._svg("")
        assert svg.startswith("<svg") or "<svg" in svg
        assert svg.rstrip().endswith("</svg>")


class TestTheMarkerIsDistinguishable:
    @pytest.mark.parametrize("real", ["W", "degC", "psi", "%RH", "L/min", "sccm", "mol"])
    def test_no_real_unit_could_be_mistaken_for_the_marker(self, real):
        """Every unit a live install actually declares."""
        assert real != UNDECLARED
        assert real != UNDECLARED_BRIEF
        assert UNDECLARED not in real


class TestTheTable:
    """A table's unit column is where a reader looks for the unit. Blank
    there is the most convincing lie of the set, because the column exists
    and appears to have been filled in."""

    def _spec(self):
        from ..table_spec import Column, TableSpec

        return TableSpec(
            kind="rows",
            title="readings",
            columns=(
                Column(id="channel", label="Channel"),
                Column(id="value", label="Value", align="right"),
                Column(id="unit", label="Unit"),
            ),
        )

    def test_a_blank_unit_cell_says_it_is_undeclared(self):
        from ..table_spec import render_text

        out = render_text(self._spec(), UNITLESS_ROWS, show_footer=False)
        assert _says_absent(out)

    def test_a_declared_unit_is_shown_as_itself(self):
        from ..table_spec import render_text

        out = render_text(self._spec(), DECLARED_ROWS, show_footer=False)
        assert not _says_absent(out)

    def test_rows_for_display_fills_the_gap_for_web_and_export(self):
        """`render_text` is not the only way rows escape — the web component
        and any export take them directly."""
        from ..table_spec import rows_for_display

        out = rows_for_display(self._spec(), UNITLESS_ROWS)
        assert all(r["unit"] == UNDECLARED for r in out)

    def test_it_does_not_mutate_the_caller_s_rows(self):
        """These are usually a query result the caller uses for other
        things."""
        from ..table_spec import rows_for_display

        original = [dict(r) for r in UNITLESS_ROWS]
        rows_for_display(self._spec(), UNITLESS_ROWS)
        assert original == UNITLESS_ROWS

    def test_a_table_with_no_unit_column_is_untouched(self):
        from ..table_spec import Column, TableSpec, rows_for_display

        spec = TableSpec(kind="rows", title="t", columns=(Column(id="channel", label="Channel"),))
        assert rows_for_display(spec, UNITLESS_ROWS) == list(UNITLESS_ROWS)


class TestExportCells:
    def test_a_csv_unit_column_is_never_blank(self):
        """A blank cell in somebody else's spreadsheet is indistinguishable
        from a quantity that needs no unit, and by then nobody is left to
        ask."""
        from ..units import for_export

        assert for_export(None) == UNDECLARED
        assert for_export("") == UNDECLARED
        assert for_export("degC") == "degC"


class TestTheIdiomThatCausedItCannotComeBack:
    """The defect was one line, written independently in four modules.

    `f" {unit}" if unit else ""` and `str(row.get("unit") or "")` each look
    harmless in isolation. Together they meant absent silently became the
    empty string everywhere a value was drawn, and no single review would
    have caught the pattern because no single file contained it twice.
    """

    #: The renderers. Not every module that mentions a unit — only the ones
    #: whose output a person reads.
    RENDERERS = ("chart_svg.py", "chart_render.py", "table_spec.py")

    #: The idiom, as it was actually written.
    IDIOM = r"""if\s+\w*unit\w*\s*else\s*(""|'')"""

    @staticmethod
    def _code_only(path):
        """Source with comments and string literals removed.

        The first version of this guard flagged its own docstring, which
        quoted the idiom in order to explain it — the same way the tier
        guard first flagged a sentence ending "derive it from silver.". A
        rule about code has to be applied to code, or the only way to
        document a defect is to stop naming it.
        """
        import tokenize

        out = []
        with open(path, "rb") as handle:
            for token in tokenize.tokenize(handle.readline):
                if token.type in (tokenize.COMMENT, tokenize.STRING):
                    continue
                out.append(token.string)
        return " ".join(out)

    def test_no_renderer_turns_an_absent_unit_into_the_empty_string(self):
        import pathlib
        import re

        pattern = re.compile(self.IDIOM)
        here = pathlib.Path(__file__).resolve().parents[1]
        offenders = [
            name for name in self.RENDERERS
            if pattern.search(self._code_only(here / name))
        ]
        assert not offenders, (
            "an absent unit must be rendered, not skipped — use "
            "units.label/annotate/qualify: " + ", ".join(offenders)
        )

    def test_the_guard_recognises_the_idiom_it_is_looking_for(self):
        """Proven against the exact text that was in chart_svg and
        chart_render, so the pattern cannot quietly stop matching
        anything."""
        import re

        pattern = re.compile(self.IDIOM)
        assert pattern.search("""f"{' ' + own_unit if own_unit else ''}\"""")
        assert pattern.search('suffix = f" {unit}" if unit else ""')

    def test_it_does_not_flag_the_corrected_form(self):
        import re

        pattern = re.compile(self.IDIOM)
        assert not pattern.search('f"{drawn} {own_unit}" if declared(own_unit) else other')

    def test_the_renderers_named_here_all_exist(self):
        import pathlib

        here = pathlib.Path(__file__).resolve().parents[1]
        missing = [n for n in self.RENDERERS if not (here / n).exists()]
        assert not missing, f"guard names files that are gone: {missing}"


class TestTheGutterFitsWhatIsDrawnInIt:
    """Saying the unit is missing exposed a layout defect underneath it.

    An undeclared unit cannot take an SI prefix — there is no unit to
    prefix — so its axis keeps full-width numbers instead of being scaled
    to "1 M". Against a fixed 68-unit left margin, a right-anchored
    "1000000" ran off the canvas and rendered as "000000", which is not a
    smaller number, it is a different one.
    """

    POINTS = [
        (datetime(2026, 9, 1, tzinfo=UTC) + timedelta(minutes=15 * i), 585000.0 + 5000.0 * i)
        for i in range(24)
    ]

    def _svg(self, unit, column="single"):
        from ..chart_svg import Geometry

        return render_svg(
            [Series("Power", self.POINTS, unit=unit)],
            title="site-a — power",
            subtitle="",
            provenance="test",
            geometry=Geometry.for_column(column),
        )

    def _x_positions(self, svg):
        import re

        return [float(m) for m in re.findall(r'<text x="(-?[\d.]+)"', svg)]

    def test_nothing_is_drawn_off_the_left_edge(self):
        assert min(self._x_positions(self._svg(""))) >= 0

    def test_a_wide_tick_label_fits_inside_the_canvas(self):
        """The label is right-anchored, so its LEFT edge is what overflows,
        and an `x >= 0` check on the anchor alone would not have caught it.

        Measured against the widest label the figure actually drew, not a
        hardcoded number — a fixture whose ticks happen to be narrow would
        make a hardcoded assertion pass while proving nothing.
        """
        import re

        from ..chart_svg import Geometry, _text_width

        svg = self._svg("")
        labels = re.findall(r'text-anchor="end"[^>]*>([^<]+)</text>', svg)
        assert labels, "no right-anchored labels in the figure"
        widest = max(labels, key=len)
        assert len(widest) >= 6, f"fixture too narrow to exercise this: {widest!r}"
        # At the size the figure actually drew them. A journal-column
        # figure scales its type down, and measuring at the unscaled 11.5
        # would assert against a layout nobody rendered.
        size = 11.5 * Geometry.for_column("single").scale
        # Every right-anchored label starts at anchor minus its own width.
        for anchor, text in zip(self._x_positions(svg), labels, strict=False):
            assert anchor - _text_width(text, size) >= -1.0, (text, anchor)

    def test_a_declared_unit_still_gets_the_narrow_gutter_it_deserves(self):
        """Scaled to MW the labels are one or two characters, and widening
        the gutter for them would waste the plot area on every normal
        figure."""
        wide = self._svg("")
        narrow = self._svg("W")
        assert len(self._x_positions(narrow)) > 0
        assert min(self._x_positions(narrow)) <= min(self._x_positions(wide))

    def test_it_holds_at_journal_column_widths(self):
        for column in ("single", "double", "screen"):
            assert min(self._x_positions(self._svg("", column))) >= 0
