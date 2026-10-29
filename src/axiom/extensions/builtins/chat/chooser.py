# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Arrow to a choice and press Enter. One implementation, every scenario.

The TUI had a picker, and it was a SESSION picker: its state held session
metadata, its rendering read session fields, and adding alerts meant a third
mode inside it. That is how a selection UI becomes three selection UIs that
drift — one grows a filter toggle, another grows multi-select, and the third
does neither.

So the model lives here and knows nothing about what is being chosen. It is
pure: no prompt_toolkit, no I/O, no callbacks. A surface owns the keys and the
paint; this owns what is selected and what the rows say. That split is what
lets it be tested exhaustively and reused by a surface that is not a
full-screen TUI.

Behaviour matches what people already expect from this shape of prompt: ↑↓ to
move, Enter to take the highlighted one, Esc to cancel, Space to toggle when
several may be chosen.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class ChooseMode(Enum):
    ONE = "one"       # Enter takes the highlighted row
    MANY = "many"     # Space toggles, Enter takes everything checked


@dataclass(frozen=True)
class Choice:
    """One row. `value` is whatever the caller needs back — never rendered.

    `cells` are the columns. A single-column chooser can pass `label` instead
    and the row becomes one cell — the common case should not have to build a
    tuple.
    """

    label: str = ""
    cells: tuple[str, ...] = ()
    detail: str = ""
    value: Any = None

    @property
    def columns(self) -> tuple[str, ...]:
        return self.cells if self.cells else (self.label,)


@dataclass
class ChooserState:
    """What is on screen and what is selected."""

    choices: list[Choice]
    mode: ChooseMode = ChooseMode.ONE
    title: str = ""
    headers: tuple[str, ...] = ()
    """Column headings. Empty renders the grid without a header row — right
    when the rows speak for themselves, wrong when a column is a bare number
    nobody can name."""
    cursor: int = 0
    checked: set[int] = field(default_factory=set)

    def __post_init__(self) -> None:
        # An empty chooser is a caller bug: there is nothing to choose, and a
        # surface that opens one shows an empty box with no way out that reads
        # as a hang.
        if not self.choices:
            raise ValueError("a chooser needs at least one choice")
        self.cursor = _clamp(self.cursor, len(self.choices))


def _clamp(index: int, count: int) -> int:
    if count <= 0:
        return 0
    return max(0, min(index, count - 1))


def move(state: ChooserState, delta: int) -> ChooserState:
    """Move the cursor. Stops at the ends rather than wrapping.

    Wrapping is wrong for a list you are reading: pressing ↓ at the bottom and
    landing at the top loses your place, and with one keypress too many you
    act on the wrong row.
    """
    state.cursor = _clamp(state.cursor + delta, len(state.choices))
    return state


def toggle(state: ChooserState) -> ChooserState:
    """Check or uncheck the highlighted row. No-op unless several may be
    chosen — Space doing nothing is better than Space doing something
    invisible."""
    if state.mode is not ChooseMode.MANY:
        return state
    if state.cursor in state.checked:
        state.checked.discard(state.cursor)
    else:
        state.checked.add(state.cursor)
    return state


def selection(state: ChooserState) -> list[Any]:
    """What Enter would take.

    In MANY mode with nothing checked, this is the highlighted row — pressing
    Enter having checked nothing means "this one", not "none", which is the
    reading every other list of this shape has.
    """
    if state.mode is ChooseMode.ONE or not state.checked:
        return [state.choices[state.cursor].value]
    return [state.choices[i].value for i in sorted(state.checked)]


def checked_selection(state: ChooserState) -> list[Any]:
    """Only what was EXPLICITLY checked. Empty means empty.

    The counterpart to :func:`selection`, and the difference matters where the
    action is destructive. "Enter with nothing checked means this one" is the
    right reading for acknowledging an alert and a dangerous one for archiving
    a session — there, an empty list must mean "never mind", because the user
    who pressed Enter having checked nothing was not aiming at the row their
    cursor happened to rest on.
    """
    return [state.choices[i].value for i in sorted(state.checked)]


def remove(state: ChooserState, values: Iterable[Any]) -> ChooserState:
    """Drop rows that have been dealt with, keeping the cursor sensible.

    Acting on a row usually means it should leave the list — an acknowledged
    alert, an archived session. Rebuilding the whole chooser would reset the
    cursor to the top and make the next action land somewhere unexpected.
    """
    gone = list(values)
    keep = [c for c in state.choices if c.value not in gone]
    if not keep:
        state.choices = []
        state.cursor = 0
        state.checked = set()
        return state
    state.choices = keep
    state.cursor = _clamp(state.cursor, len(keep))
    state.checked = set()
    return state


def is_empty(state: ChooserState) -> bool:
    """Whether everything has been dealt with — the surface should close."""
    return not state.choices


def hint(mode: ChooseMode) -> str:
    """The key legend. One wording everywhere, so the keys read as universal
    rather than as this screen's own invention."""
    if mode is ChooseMode.MANY:
        return "↑↓ navigate · Space toggle · Enter confirm · Esc cancel"
    return "↑↓ navigate · Enter select · Esc cancel"


def render_lines(state: ChooserState, width: int = 72) -> list[str]:
    """The chooser as a bordered grid — the same table every other neut surface
    draws.

    Deliberately not picker-specific formatting. This session built one table
    primitive precisely so a list of things does not look different depending
    on which screen it is on, and a selection overlay is a list of things. The
    cursor is a column, so the pointer sits in its own lane instead of shifting
    the row it points at.

    Returned as lines rather than a string so the surface can style per line:
    the TUI's lexer works line by line and already paints a pre-rendered grid
    as a drawn object.
    """
    from axiom.infra.cli_format import DEFAULT_BORDER, Column, table

    width_of_row = max(len(c.columns) for c in state.choices) if state.choices else 1
    headers = list(state.headers) if state.headers else [""] * width_of_row

    # The cursor is a COLUMN, so the pointer sits in its own lane rather than
    # shifting the row it points at — a row that moves as you arrow over it is
    # the thing that makes a list feel unsteady.
    columns = [Column(header="", align="left")]
    if state.mode is ChooseMode.MANY:
        columns.append(Column(header="", align="left"))
    for index, header in enumerate(headers):
        # The last column wraps: it is the one carrying prose, and starving it
        # is what tears a sentence across two lines at the wrong indent.
        columns.append(Column(header=header, wrap=index == len(headers) - 1))

    rows: list[list[str]] = []
    for index, choice in enumerate(state.choices):
        row = ["›" if index == state.cursor else " "]
        if state.mode is ChooseMode.MANY:
            row.append("[x]" if index in state.checked else "[ ]")
        cells = list(choice.columns)
        cells += [""] * (width_of_row - len(cells))
        rows.append(row + cells)

    lines: list[str] = []
    if state.title:
        lines.append(f"  {state.title}")
    try:
        lines.extend(
            table(
                rows,
                columns,
                width=width,
                # The standard margin is the default; restating it is how a
                # table drifts from the column every other one starts in.
                headers=bool(state.headers),
                border=DEFAULT_BORDER,
            )
        )
    except ValueError:
        # The table refuses a layout it cannot draw honestly, which is right —
        # but a chooser that raises takes the whole surface down, and the user
        # is left with a keypress that did nothing. Fall back to plain rows:
        # less beautiful, still usable, and it never strands anyone.
        for index, choice in enumerate(state.choices):
            pointer = " › " if index == state.cursor else "   "
            box = ""
            if state.mode is ChooseMode.MANY:
                box = "[x] " if index in state.checked else "[ ] "
            lines.append(f"  {pointer}{box}{' · '.join(c for c in choice.columns if c)}")
    lines.append(f"  {hint(state.mode)}")

    # The highlighted row's detail, below the frame rather than inside it: it
    # changes as the cursor moves, and a cell that changes height would make
    # the whole grid jump.
    detail = state.choices[state.cursor].detail if state.choices else ""
    if detail:
        lines.append(f"  └ {detail}")
    return lines


__all__ = [
    "Choice",
    "ChooseMode",
    "ChooserState",
    "checked_selection",
    "hint",
    "is_empty",
    "move",
    "remove",
    "render_lines",
    "selection",
    "toggle",
]
