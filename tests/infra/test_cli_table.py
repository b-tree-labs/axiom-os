# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""A table that knows how wide it is allowed to be.

Every table in this codebase was hand-rolled with `ljust`, sized to its own
content, and handed to a terminal that then hard-wrapped it. `/planes` is the
worked example: it produced

    domain   unconfigured  -                    no
      retrieval store: set rag.database_url (or DATABASE_URL)
    trace    unconfigured  -                    follows the
      domain plane: nothing to log

which reads as "no" and "follows the" — the sentence torn in half by a wrap
the table never knew was coming, with the remainder dumped at an indent that
belongs to nothing.

A table has to own its width. That means: fit the columns to a budget, wrap
inside the column that is allowed to grow, and indent every continuation line
to that column's own left edge so a wrapped cell still reads as one cell.
"""

from __future__ import annotations

import pytest

from axiom.infra.cli_format import Column, table

ROWS = [
    ("domain", "unconfigured", "-", "no retrieval store: set rag.database_url (or DATABASE_URL)"),
    (
        "person",
        "local",
        "os-session @dev:local",
        "posture open (unproven); memory principal @laptop:dev",
    ),
    (
        "session",
        "local",
        "/home/dev/.axi/memory/artifacts.db",
        "memory principal @laptop:dev (present)",
    ),
]
COLS = [Column("plane"), Column("kind"), Column("source", wrap=True), Column("detail", wrap=True)]


class TestItFitsTheBudget:
    def test_no_line_exceeds_the_width(self):
        for line in table(ROWS, COLS, width=72):
            assert len(line) <= 72, f"{len(line)} > 72: {line!r}"

    def test_a_budget_too_narrow_says_so_instead_of_mangling(self):
        """Four columns cannot live in thirty characters. Saying so beats
        emitting something unreadable and calling it a table."""
        with pytest.raises(ValueError, match="widen"):
            table(ROWS, COLS, width=30)

    def test_no_trailing_whitespace_anywhere(self):
        for line in table(ROWS, COLS, width=72):
            assert line == line.rstrip(), f"trailing space: {line!r}"


class TestAWrappedCellStaysOneCell:
    def _detail_column_start(self, lines):
        """Where the detail column begins on the first row."""
        return lines[0].index("no retrieval store")

    def test_continuations_align_to_their_column(self):
        """The defect, stated as a test.

        A continuation at a different indent from its own column reads as a
        new field rather than the rest of the previous one.
        """
        lines = table(ROWS, COLS, width=60)
        start = self._detail_column_start(lines)
        for line in lines:
            if not line.strip():
                continue
            # A continuation line is one whose first non-space character sits
            # at or beyond the detail column and which carries no other cell.
            stripped = line.lstrip()
            indent = len(line) - len(stripped)
            if indent >= start:
                assert indent == start, (
                    f"continuation indented to {indent}, detail column is {start}: {line!r}"
                )

    def test_a_long_token_is_kept_whole_when_it_fits(self):
        """`rag.database_ur` / `l` is not a smaller piece of the value."""
        lines = table(ROWS, COLS, width=78)
        joined = " ".join(" ".join(line.split()) for line in lines)
        assert "rag.database_url" in joined, "token was split:\n" + "\n".join(lines)

    def test_a_token_wider_than_its_column_is_elided_not_overhung(self):
        """An overhang shoves the next column out of its lane, which destroys
        the alignment the table exists for. Eliding keeps the lane and marks
        the cut, so nobody reads a shortened path as the whole path."""
        lines = table(ROWS, COLS, width=78)
        path_line = next(ln for ln in lines if "/home" in ln)
        assert "…" in path_line, f"expected an elision marker in {path_line!r}"
        assert "/home/dev" in path_line, "the head of the path survives"
        assert "ifacts.db" in path_line, "the filename end survives"
        assert len(path_line) <= 78, "and it stays inside the budget"

    def test_elision_keeps_both_ends_of_a_path(self):
        from axiom.infra.cli_format import elide

        out = elide("/home/dev/.axi/memory/artifacts.db", 20)
        assert len(out) == 20
        assert out.startswith("/home"), "the root survives"
        assert out.endswith(".db"), "the filename survives"
        assert "…" in out

    def test_hyphenated_values_are_not_broken_at_the_hyphen(self):
        rows = [("a", "b", "c", "os-session @dev:local is a compound-token value here")]
        joined = " ".join(" ".join(ln.split()) for ln in table(rows, COLS, width=56))
        assert "os-session" in joined and "compound-token" in joined

    def test_a_cell_that_fits_is_left_alone(self):
        rows = [("a", "b", "c", "short")]
        lines = table(rows, COLS, width=80)
        assert len(lines) == 1
        assert lines[0].strip().endswith("short")


class TestTheFixedColumnsStayAligned:
    def test_every_row_starts_its_columns_in_the_same_place(self):
        lines = table(ROWS, COLS, width=100)
        # Row-opening lines are the ones beginning with a plane name.
        starts = [ln.index(r[0]) for ln in lines for r in ROWS if ln.lstrip().startswith(r[0])]
        assert len(set(starts)) == 1, f"row starts diverge: {sorted(set(starts))}"

    def test_a_long_value_widens_its_column_for_everyone(self):
        rows = [("a", "x", "s", "d"), ("bbbbbbbb", "y", "t", "e")]
        lines = table(rows, COLS, width=80)
        assert lines[0].index("x") == lines[1].index("y")


class TestItRefusesToLie:
    def test_an_impossible_budget_raises_rather_than_silently_truncating(self):
        """Truncating a value is a quiet wrong answer, which is worse than a
        loud refusal — the caller can choose a narrower column set."""
        with pytest.raises(ValueError):
            table(ROWS, COLS, width=8)

    def test_headers_are_optional_and_off_by_default(self):
        lines = table(ROWS, COLS, width=80)
        assert "plane" not in lines[0], "headers only when asked for"

    def test_headers_are_centred_over_their_column_not_left_aligned(self):
        """A left-aligned header on a wide column drifts away from the data
        it names and the eye stops associating the two."""
        lines = table(ROWS, COLS, width=100, headers=True)
        header, data = lines[0], lines[2]
        # "plane" is narrow enough that centring and left-aligning coincide;
        # the wide "detail" column is where the difference shows.
        assert header.index("detail") > data.index("no retrieval"), (
            "the header should sit centred, to the right of the cell's left edge"
        )


class TestTheBorderedGrid:
    """The look Ben specified: full grid, rule between every row, centred
    headers, narrow columns sized to content and the prose column taking what
    is left."""

    GRID = [
        ("#169", "9/4", "Not docs — a real fix, and the one that matters."),
        ("#174", "9/4", "Spec: DAQ source onboarding, data-driven and no code"),
        (
            "#140",
            "8/16",
            "Austin's — folds the ExpMan prototype walkthrough, operator "
            "questionnaire and DT presentation into the PRD",
        ),
    ]
    GCOLS = [Column("PR"), Column("Age"), Column("What it is", wrap=True)]

    def _grid(self, width=100):
        from axiom.infra.cli_format import SQUARE

        # indent=0 so these assert about the BOX, not where it sits. The left
        # margin is a separate concern with its own tests; without this every
        # edge assertion here would also be an assertion about the margin.
        return table(
            self.GRID,
            self.GCOLS,
            width=width,
            indent=0,
            headers=True,
            border=SQUARE,
            rules=True,
        )

    def test_it_is_closed_on_all_four_sides(self):
        lines = self._grid()
        assert lines[0].startswith("┌") and lines[0].endswith("┐")
        assert lines[-1].startswith("└") and lines[-1].endswith("┘")
        for line in lines[1:-1]:
            assert line[0] in "│├", f"open left edge: {line!r}"
            assert line[-1] in "│┤", f"open right edge: {line!r}"

    def test_there_is_a_rule_between_every_row(self):
        lines = self._grid()
        rules = [ln for ln in lines if ln.startswith("├")]
        # one under the header, one between each adjacent pair of rows
        assert len(rules) == len(self.GRID), f"expected {len(self.GRID)} rules, got {len(rules)}"

    def test_headers_are_centred_over_their_column(self):
        lines = self._grid()
        header = lines[1]
        cell = header.split("│")[3]  # the wide column
        left = len(cell) - len(cell.lstrip())
        right = len(cell) - len(cell.rstrip())
        assert abs(left - right) <= 1, f"header not centred: {cell!r}"

    def test_every_line_is_the_same_width(self):
        lines = self._grid()
        assert len({len(ln) for ln in lines}) == 1, (
            f"ragged frame: {sorted({len(ln) for ln in lines})}"
        )

    def test_the_frame_is_charged_to_the_budget(self):
        """The border costs width. If it were not charged, the frame would
        push the table past the terminal edge — the exact failure the width
        discipline exists to prevent."""
        for w in (100, 80, 64):
            for line in self._grid(width=w):
                assert len(line) <= w, f"width={w} produced {len(line)}: {line!r}"

    def test_a_wrapped_cell_stays_inside_its_box(self):
        lines = self._grid(width=80)
        wrapped = [ln for ln in lines if "presentation into the PRD" in ln]
        assert wrapped, "the long row should have wrapped"
        assert wrapped[0].startswith("│") and wrapped[0].endswith("│")

    def test_borderless_is_still_available_for_the_tui(self):
        """prompt-toolkit eats verticals and drops characters, verified by
        driving the real TUI. That surface gets aligned columns, no frame."""
        lines = table(self.GRID, self.GCOLS, width=80)
        assert not any(ch in "│┌┐└┘├┤┬┴┼" for ln in lines for ch in ln)


class TestColourDoesNotBreakAlignment:
    """Escape codes occupy no columns.

    A table measuring raw `len` pads a coloured cell by the width of its
    escape sequences, and every column after it walks off. The call sites in
    this codebase currently hand-compensate — `{label:<{pad + len(coloured) -
    len(plain)}}` — which is the shape of a primitive that should have been
    doing it for them.
    """

    RED = "\x1b[31m"
    OFF = "\x1b[0m"

    def _c(self, text):
        return f"{self.RED}{text}{self.OFF}"

    def test_a_coloured_cell_occupies_its_visible_width(self):
        from axiom.infra.cli_format import visible_len

        rows = [(self._c("running"), "a"), ("done", "b")]
        cols = [Column("status"), Column("what", wrap=True)]
        lines = table(rows, cols, width=40)
        # Both rows put the second column at the same visible offset.
        offsets = [
            visible_len(ln.split("a")[0]) if "a" in ln else visible_len(ln.split("b")[0])
            for ln in lines
        ]
        assert len(set(offsets)) == 1, f"coloured row misaligned: {offsets}"

    def test_visible_len_ignores_escape_codes(self):
        from axiom.infra.cli_format import visible_len

        assert visible_len(self._c("running")) == len("running")
        assert visible_len("plain") == 5

    def test_the_budget_counts_visible_columns_not_bytes(self):
        from axiom.infra.cli_format import visible_len

        rows = [(self._c("running"), "some detail text that will need to wrap here")]
        cols = [Column("status"), Column("detail", wrap=True)]
        for line in table(rows, cols, width=40):
            assert visible_len(line) <= 40, f"{visible_len(line)} visible cols: {line!r}"

    def test_colour_survives_the_round_trip(self):
        rows = [(self._c("running"), "x")]
        cols = [Column("status"), Column("what", wrap=True)]
        assert self.RED in "\n".join(table(rows, cols, width=40))


class TestACapKeepsOneOddRowFromCostingTheTable:
    """`max_width` on a column whose real content is bounded.

    Without it, a single unexpectedly long value in a non-wrapping column has
    no give: the layout cannot be satisfied and the table refuses entirely.
    Refusing is right when the columns genuinely do not fit, but wrong when
    one row is anomalous and the rest are fine.
    """

    def test_a_capped_column_does_not_take_the_table_down(self):
        rows = [
            ("1", "abc123", "a normal title"),
            ("2", "x" * 400, "another title"),
        ]
        cols = [Column("#"), Column("id", max_width=24), Column("title", wrap=True)]
        lines = table(rows, cols, width=76)
        assert lines, "one anomalous value should not cost everyone the table"
        for line in lines:
            assert len(line) <= 76

    def test_the_capped_value_is_elided_not_silently_shortened(self):
        rows = [("1", "y" * 400, "t")]
        cols = [Column("#"), Column("id", max_width=24), Column("title", wrap=True)]
        assert "…" in "\n".join(table(rows, cols, width=76))

    def test_a_cap_does_not_widen_a_short_column(self):
        rows = [("1", "ab", "t")]
        cols = [Column("#"), Column("id", max_width=24), Column("title", wrap=True)]
        line = table(rows, cols, width=76)[0]
        assert line.index("t") - line.index("ab") < 10, "cap is a ceiling, not a width"


class TestWidthIsMeasuredInColumnsNotCodepoints:
    """A status glyph is two columns wide and one codepoint long.

    Padding by ``len()`` puts the border two places early for every such
    cell, so a single ``✅`` in a column breaks the whole table. This is the
    reason the status vocabulary is defined once and every glyph in it is the
    same width.
    """

    def test_a_wide_glyph_counts_as_two_columns(self):
        from axiom.infra.cli_format import visible_len

        assert visible_len("✅") == 2
        assert visible_len("a✅b") == 4

    def test_an_emoji_presentation_selector_does_not_add_a_column(self):
        """``⚠️`` is two codepoints — the sign plus U+FE0F — and two columns,
        not three. The selector asks for emoji presentation; it is not itself
        drawn."""
        from axiom.infra.cli_format import visible_len

        assert visible_len("⚠️") == 2
        assert len("⚠️") == 2

    def test_a_narrow_check_is_still_one_column(self):
        from axiom.infra.cli_format import visible_len

        assert visible_len("✓") == 1

    def test_ansi_colour_still_occupies_nothing(self):
        from axiom.infra.cli_format import visible_len

        assert visible_len("\x1b[31m✅\x1b[0m") == 2

    def test_every_status_glyph_is_the_declared_width(self):
        """They are interchangeable only if they measure the same. A
        one-column mark beside a two-column one puts the border in two
        different places on consecutive rows."""
        from axiom.infra.cli_format import Glyph, visible_len

        for name in ("OK", "FAIL", "WARN", "UNKNOWN", "ABSENT", "PENDING"):
            glyph = getattr(Glyph, name)
            assert visible_len(glyph) == Glyph.WIDTH, (
                f"Glyph.{name} is {visible_len(glyph)} columns, not {Glyph.WIDTH}"
            )

    def test_a_table_of_wide_glyphs_keeps_its_columns(self):
        from axiom.infra.cli_format import SQUARE, Column, Glyph, table, visible_len

        rows = [
            (Glyph.OK, "configured", "ready"),
            (Glyph.ABSENT, "not configured", "optional"),
            (Glyph.WARN, "expired", "renew the credential"),
            ("", "no glyph at all", "still lines up"),
        ]
        lines = table(
            rows,
            [Column(""), Column("name"), Column("detail")],
            width=60,
            headers=True,
            border=SQUARE,
        )
        widths = {visible_len(ln) for ln in lines}
        assert len(widths) == 1, f"rows differ in width: {sorted(widths)}"


class TestEveryTableStartsInTheSameColumn:
    """Tables drifted to three different left margins — some flush, some at
    two, one at four — so consecutive commands disagreed on where the screen
    begins. The margin is the default now rather than a per-call argument,
    which is what stops the next caller from picking a fourth.
    """

    def test_the_default_margin_is_the_standard_one(self):
        from axiom.infra.cli_format import _STANDARD_INDENT, Column, table

        lines = table([("a", "b")], [Column("x"), Column("y")], width=40)
        assert all(ln.startswith(" " * _STANDARD_INDENT) for ln in lines if ln.strip())
        assert not lines[0].startswith(" " * (_STANDARD_INDENT + 1))

    def test_a_caller_can_still_ask_for_something_else(self):
        from axiom.infra.cli_format import Column, table

        lines = table([("a", "b")], [Column("x"), Column("y")], width=40, indent=0)
        assert lines[0][0] != " "

    def test_no_production_caller_overrides_the_margin(self):
        """A per-call indent is how the drift happened. If one is genuinely
        needed the assertion should be relaxed deliberately, not by accident.
        """
        import pathlib
        import re

        root = pathlib.Path(__file__).resolve().parents[2] / "src" / "axiom"
        offenders = []
        for path in root.rglob("*.py"):
            if "tests" in path.parts:
                continue
            text = path.read_text(encoding="utf-8", errors="ignore")
            # Strip comments before scanning. A comment SAYING "no indent=" is
            # not a caller passing one, and flagging it taught exactly the
            # wrong lesson: the fix would have been to reword the comment
            # rather than to stop overriding the margin.
            text = re.sub(r"#[^\n]*", "", text)
            for match in re.finditer(r"\btable\(\s*(.{0,400}?)\)", text, re.S):
                if re.search(r"\bindent=", match.group(1)):
                    offenders.append(f"{path.relative_to(root)}")
        assert not offenders, f"tables overriding the standard margin: {sorted(set(offenders))}"
