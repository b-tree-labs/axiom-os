# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Reusable CLI formatting helpers for consistent terminal output.

Provides section headers, separators, Unicode boxes, key-value lines and a
width-aware table, so every extension renders with the same visual style.
"""

from __future__ import annotations

import re
import shutil
import textwrap
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime


@dataclass(frozen=True)
class Column:
    """One column of a :func:`table`.

    ``wrap`` marks a column that may give up width and wrap its contents. Any
    number of columns may wrap; the leftover budget is shared between them in
    proportion to what they actually need, so a long path does not starve the
    prose beside it.

    ``max_width`` caps a column regardless of content — useful for a value
    that is occasionally enormous and usually short.
    """

    header: str = ""
    wrap: bool = False
    align: str = "left"
    max_width: int | None = None


#: Colour codes occupy no columns. A table that measures raw ``len`` pads a
#: coloured cell by the width of its escape sequences and every column after
#: it walks off. Call sites currently hand-compensate for this; they should
#: not have to.
_ANSI = re.compile(r"\x1b\[[0-9;]*m")


class Glyph:
    """The one status vocabulary for every interactive surface.

    Before this there were at least five: ``✓/✗``, ``✅/❌``, ``○/●``,
    ``⚠/⚠️`` and bare ``?``, mixed within single screens — `axi status` drew
    ``✓`` in its service lines and ``✅`` in its verdict two inches below.

    Every glyph here is **exactly two columns wide**, which is what makes
    them interchangeable: a one-column ``✓`` and a two-column ``✅`` in the
    same table column put the border in different places. None of them is a
    bare mark that a terminal might render either way — where a variation
    selector is involved (``⚠️``) the width is still two, and
    :func:`visible_len` measures it as such.

    Named ``Glyph`` rather than ``Status`` because several modules already
    have a ``Status`` of their own, and a shared vocabulary that collides
    with local names is one people alias differently in every file.

    Use the names, not the characters. A screen that spells its own glyphs
    is a screen that drifts.
    """

    #: Working, present, configured, passed.
    OK = "✅"
    #: Broken, failed, refused.
    FAIL = "❌"
    #: Working but not fully — degraded, deprecated, needs attention.
    WARN = "⚠️"
    #: Could not be determined. Not the same as absent, and not a failure.
    UNKNOWN = "❓"
    #: Deliberately not present — optional, unconfigured, skipped.
    ABSENT = "⬜"
    #: In flight.
    PENDING = "⏳"

    #: Width every glyph above occupies, so callers can reserve a column.
    WIDTH = 2


def relative_age(timestamp: str) -> str:
    """A timestamp as an age, because an age is the question being asked.

    "2026-09-07T01:10:48" costs nineteen columns to say something the reader
    then has to compute. "6h ago" says it. The exact value stays in `--json`,
    which is where a machine reads it from anyway.
    """
    if not timestamp:
        return "unknown"

    try:
        when = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        if when.tzinfo is None:
            when = when.replace(tzinfo=UTC)
        seconds = (datetime.now(UTC) - when).total_seconds()
    except (ValueError, TypeError):
        return timestamp[:10]

    if seconds < 0:
        return "just now"
    for limit, divisor, unit in (
        (90, 1, "s"),
        (5400, 60, "m"),
        (172800, 3600, "h"),
        (None, 86400, "d"),
    ):
        if limit is None or seconds < limit:
            return f"{int(seconds // divisor)}{unit} ago"
    return timestamp[:10]


def char_width(char: str) -> int:
    """Columns a single character occupies on screen.

    Counting codepoints is wrong the moment a glyph is not Latin. ``✅`` is
    one codepoint and two columns; a combining mark is one codepoint and
    none. A table that pads by ``len()`` puts its border in the wrong place
    for every such cell, which is how a status icon breaks a whole column.

    Uses the stdlib rather than taking a dependency for it: East Asian Wide
    and Fullwidth are the two-column classes, and marks and format
    characters take no space of their own.
    """
    if unicodedata.combining(char) or unicodedata.category(char) in {"Mn", "Me", "Cf"}:
        return 0
    return 2 if unicodedata.east_asian_width(char) in {"W", "F"} else 1


#: U+FE0F. Requests emoji presentation for the character before it, which
#: makes that character two columns wide even when its own East Asian class
#: says one — ``⚠`` is narrow, ``⚠️`` is not.
_EMOJI_PRESENTATION = "\ufe0f"


def visible_len(text: str) -> int:
    """Width of ``text`` on screen, ignoring ANSI colour codes."""
    plain = _ANSI.sub("", text)
    total = 0
    for index, char in enumerate(plain):
        # The selector itself is a format character, so `char_width` already
        # scores it zero — it needs no case of its own. What it does need is
        # to widen the character BEFORE it.
        if plain[index + 1 : index + 2] == _EMOJI_PRESENTATION:
            total += 2
        else:
            total += char_width(char)
    return total


def _vpad(text: str, width: int, align: str = "left") -> str:
    """Pad to ``width`` visible columns, preserving any colour codes."""
    gap = width - visible_len(text)
    if gap <= 0:
        return text
    return (" " * gap + text) if align == "right" else (text + " " * gap)


def terminal_width(reserve: int = 0) -> int:
    """Usable columns, from the real terminal, minus ``reserve`` for chrome.

    A hardcoded width is a guess that is wrong on every terminal but one. The
    chat pane, for instance, is the terminal less a scrollbar, a gutter and a
    wrap margin; a table built to 78 in an 80-column window overflows by two
    and wraps its own right edge onto the next line.
    """
    return max(40, shutil.get_terminal_size((80, 24)).columns - reserve)


_GAP = 2

#: Every table sits at the same left margin, which is where the prose around
#: it sits too. This was a per-call argument and drifted to three different
#: values — some tables flush left, some at two, one at four — so consecutive
#: commands disagreed on where the screen begins. A caller that genuinely
#: wants something else still passes `indent=`.
_STANDARD_INDENT = 2
#: Below this a column stops being a column and becomes a stack of fragments.
_MIN_COL = 8


@dataclass(frozen=True)
class BorderStyle:
    """The glyphs a bordered table draws itself with.

    ``ROUNDED`` is the default for a terminal. ``ASCII`` exists for anything
    that mangles box-drawing characters, and ``NONE`` is the plain aligned
    columns that were here before borders.
    """

    h: str = "─"
    v: str = "│"
    tl: str = "╭"
    tr: str = "╮"
    bl: str = "╰"
    br: str = "╯"
    lt: str = "├"
    rt: str = "┤"
    tt: str = "┬"
    bt: str = "┴"
    cross: str = "┼"


SQUARE = BorderStyle(tl="┌", tr="┐", bl="└", br="┘")
ROUNDED = BorderStyle()
#: What a bordered table uses unless the caller says otherwise.
DEFAULT_BORDER = SQUARE
ASCII = BorderStyle(
    h="-", v="|", tl="+", tr="+", bl="+", br="+", lt="+", rt="+", tt="+", bt="+", cross="+"
)


def elide(text: str, width: int) -> str:
    """Shorten ``text`` to ``width`` from the middle, marking the cut.

    Head and tail both survive, which is what makes a path still readable:
    ``/home/dev/.axi/memory/artifacts.db`` becomes
    ``/home/dev/…ifacts.db`` — you keep the root and the filename, the
    two parts anyone actually reads. Truncating from the right would leave
    every path in a directory looking identical.

    The ``…`` is the point. It says something was removed, which is the
    difference between eliding and quietly reporting a shorter value.
    """
    if width <= 0 or visible_len(text) <= width:
        return text
    if width == 1:
        return "…"
    keep = width - 1
    head = (keep + 1) // 2
    tail = keep - head
    return text[:head] + "…" + (text[len(text) - tail :] if tail else "")


def _vcenter(text: str, width: int) -> str:
    """Centre to ``width`` visible columns, preserving colour codes."""
    gap = width - visible_len(text)
    if gap <= 0:
        return text
    left = gap // 2
    return " " * left + text + " " * (gap - left)


def _wrap_cell(text: str, width: int) -> list[str]:
    """Wrap one cell to ``width``, never splitting a token.

    A URL or dotted path is one token: splitting it yields
    ``rag.database_ur`` / ``l``, which is not a smaller piece of the value but
    an unusable one. So tokens are kept whole — and a token that is wider than
    its column on its own is elided from the middle rather than allowed to
    overhang, because an overhang shoves the neighbouring column out of its
    lane and destroys the alignment the table exists for.
    """
    if not text:
        return [""]
    width = max(width, 1)
    pieces = textwrap.wrap(text, width, break_long_words=False, break_on_hyphens=False) or [""]
    return [p if visible_len(p) <= width else elide(p, width) for p in pieces]


def table(
    rows: Sequence[Sequence[object]],
    columns: Sequence[Column],
    *,
    width: int = 80,
    indent: int = _STANDARD_INDENT,
    headers: bool = False,
    border: BorderStyle | None = None,
    rules: bool = False,
) -> list[str]:
    """Render ``rows`` as aligned columns that fit inside ``width``.

    The problem this exists to solve: a table sized only to its own content is
    handed to a terminal that hard-wraps it, tearing a sentence in half and
    dumping the remainder at an indent belonging to no column. The result reads
    as two fragments rather than one cell.

    So the table owns its width. Columns that cannot wrap are sized to their
    widest value. The remaining budget is shared among the wrapping columns in
    proportion to what they need, and each wraps within itself — every
    continuation line indented to its own column's left edge, so a wrapped cell
    still reads as one cell and its neighbours stay in their lanes.

    Raises ``ValueError`` when no honest layout exists. Truncating instead
    would be a quiet wrong answer; the caller can drop a column, mark one
    wrappable, or widen the output.
    """
    if not rows:
        return []
    if not columns:
        raise ValueError("a table needs at least one column")

    cells = [[("" if c is None else str(c)) for c in row] for row in rows]
    for row in cells:
        if len(row) != len(columns):
            raise ValueError(f"row has {len(row)} cells but {len(columns)} columns were declared")

    natural = []
    for i, col in enumerate(columns):
        wide = max((visible_len(r[i]) for r in cells), default=0)
        if headers:
            wide = max(wide, visible_len(col.header))
        if col.max_width is not None:
            wide = min(wide, col.max_width)
        natural.append(wide)

    # A bordered table spends width on its own frame: one vertical per gutter
    # plus the two outer edges, and a space of padding either side of each
    # cell. Charge that to the budget so the frame cannot push the content
    # past the terminal edge — the failure the whole width discipline exists
    # to prevent.
    if border is not None:
        gaps = 3 * (len(columns) - 1) + 4
    else:
        gaps = _GAP * (len(columns) - 1)
    budget = width - indent - gaps
    if budget < _MIN_COL * len(columns):
        raise ValueError(
            f"width={width} cannot hold {len(columns)} columns at {_MIN_COL} each "
            f"plus {gaps} of gutter; drop a column or widen the output."
        )

    widths = list(natural)
    over = sum(widths) - budget
    if over > 0:
        elastic = [i for i, c in enumerate(columns) if c.wrap]
        if not elastic:
            raise ValueError(
                f"the columns need {sum(natural)} of {budget} available and none "
                "may wrap; mark one wrappable or widen the output."
            )
        # Take the overage from the wrapping columns in proportion to their
        # natural size, so the widest gives up the most, and never below the
        # floor that keeps a column a column.
        pool = sum(natural[i] for i in elastic)
        for i in elastic:
            share = int(round(over * (natural[i] / pool))) if pool else 0
            widths[i] = max(_MIN_COL, natural[i] - share)
        # Rounding may leave us a column or two over; trim the widest wrapper.
        while sum(widths) > budget:
            i = max(elastic, key=lambda j: widths[j])
            if widths[i] <= _MIN_COL:
                raise ValueError(
                    f"width={width} leaves no honest layout for these columns; "
                    "drop one or widen the output."
                )
            widths[i] -= 1

    def _pad(text: str, i: int) -> str:
        return _vpad(text, widths[i], columns[i].align)

    lines: list[str] = []
    lead = " " * indent

    def _rule(left: str, mid: str, right: str) -> str:
        b = border
        assert b is not None
        return lead + left + mid.join(b.h * (widths[i] + 2) for i in range(len(columns))) + right

    def _row(parts: list[str]) -> str:
        if border is None:
            return (lead + (" " * _GAP).join(parts)).rstrip()
        b = border
        return lead + b.v + b.v.join(f" {p} " for p in parts) + b.v

    if border is not None:
        lines.append(_rule(border.tl, border.tt, border.tr))

    if headers:
        # Headers centre over their column. A left-aligned header on a wide
        # column drifts away from the data it names, and the eye stops
        # associating the two.
        lines.append(_row([_vcenter(columns[i].header, widths[i]) for i in range(len(columns))]))
        if border is not None:
            lines.append(_rule(border.lt, border.cross, border.rt))
        else:
            lines.append(
                (lead + (" " * _GAP).join("─" * widths[i] for i in range(len(columns)))).rstrip()
            )

    for row_no, row in enumerate(cells):
        if rules and row_no and border is not None:
            lines.append(_rule(border.lt, border.cross, border.rt))
        pieces = [_wrap_cell(row[i], widths[i]) for i in range(len(columns))]
        height = max(len(p) for p in pieces)
        for line_no in range(height):
            parts = [
                _pad(pieces[i][line_no] if line_no < len(pieces[i]) else "", i)
                for i in range(len(columns))
            ]
            lines.append(_row(parts))

    if border is not None:
        lines.append(_rule(border.bl, border.bt, border.br))

    return lines


def section_header(title: str, char: str = "=", width: int = 80) -> str:
    """Return a section header string.

    Example output::

        ════════════════════════════════════════════════════════════════════════════════
        Title Here
        ════════════════════════════════════════════════════════════════════════════════
    """
    rule = char * width
    return f"{rule}\n{title}\n{rule}"


def separator(char: str = "─", width: int = 80) -> str:
    """Return a horizontal separator line."""
    return char * width


def box(title: str, rows: list[str], width: int = 52) -> str:
    """Return a Unicode box.

    Example output::

        ╭─ Title ──────────────────────────────────────────╮
        │  Row 1                                           │
        │  Row 2                                           │
        ╰──────────────────────────────────────────────────╯

    *width* is the total outer width including the border characters.
    """
    # Top border: ╭─ Title ─…─╮
    inner = width - 2  # space between ╭ and ╮
    title_segment = f"─ {title} "
    top = "╭" + title_segment + "─" * (inner - len(title_segment)) + "╮"

    # Content rows
    body_lines: list[str] = []
    for row in rows:
        # Pad or truncate to fit inside the box
        padded = f"  {row}"
        if len(padded) > inner:
            padded = padded[: inner - 1] + "…"
        body_lines.append("│" + padded.ljust(inner) + "│")

    # Bottom border
    bottom = "╰" + "─" * inner + "╯"

    return "\n".join([top, *body_lines, bottom])


def kv_line(key: str, value: str, key_width: int = 20) -> str:
    """Return a formatted key-value line.

    Example::

        Key:                Value
    """
    label = f"{key}:"
    return f"  {label:<{key_width}}{value}"
