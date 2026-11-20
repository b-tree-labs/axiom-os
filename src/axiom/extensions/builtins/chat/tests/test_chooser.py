# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""One selection model, exhaustively tested, reused everywhere.

The TUI's picker was a SESSION picker — its state held session metadata and
adding alerts meant a third mode inside it. That is how one selection UI
becomes three that drift. This model knows nothing about what is being chosen,
which is what makes it testable without a terminal and reusable by a surface
that is not a full-screen TUI.
"""

from __future__ import annotations

import pytest

from axiom.extensions.builtins.chat.chooser import (
    Choice,
    ChooseMode,
    ChooserState,
    hint,
    is_empty,
    move,
    remove,
    render_lines,
    selection,
    toggle,
)


def _state(n=3, mode=ChooseMode.ONE) -> ChooserState:
    return ChooserState(
        choices=[Choice(label=f"row {i}", detail=f"detail {i}", value=i) for i in range(n)],
        mode=mode,
        title="Pick one",
    )


class TestMovement:
    def test_arrows_move_the_cursor(self):
        s = _state()
        assert move(s, 1).cursor == 1
        assert move(s, 1).cursor == 2
        assert move(s, -1).cursor == 1

    def test_it_stops_at_the_ends_rather_than_wrapping(self):
        """Wrapping loses your place: ↓ at the bottom landing at the top means
        one keypress too many acts on the wrong row."""
        s = _state()
        assert move(s, -1).cursor == 0
        s.cursor = 2
        assert move(s, 1).cursor == 2

    def test_a_large_jump_is_clamped(self):
        assert move(_state(), 99).cursor == 2
        assert move(_state(), -99).cursor == 0


class TestSelection:
    def test_enter_takes_the_highlighted_row(self):
        s = _state()
        move(s, 1)
        assert selection(s) == [1]

    def test_space_does_nothing_when_only_one_may_be_chosen(self):
        """Better than doing something invisible."""
        s = _state()
        toggle(s)
        assert s.checked == set()
        assert selection(s) == [0]

    def test_space_toggles_when_several_may_be_chosen(self):
        s = _state(mode=ChooseMode.MANY)
        toggle(s)
        move(s, 2)
        toggle(s)
        assert selection(s) == [0, 2]

    def test_toggling_twice_unchecks(self):
        s = _state(mode=ChooseMode.MANY)
        toggle(s)
        toggle(s)
        assert s.checked == set()

    def test_enter_with_nothing_checked_takes_the_highlighted_row(self):
        """"This one", not "none" — the reading every list of this shape has."""
        s = _state(mode=ChooseMode.MANY)
        move(s, 1)
        assert selection(s) == [1]


class TestActingOnRows:
    def test_a_dealt_with_row_leaves_the_list(self):
        s = _state()
        remove(s, [1])
        assert [c.value for c in s.choices] == [0, 2]

    def test_the_cursor_stays_sensible_after_removal(self):
        """Rebuilding the chooser would reset to the top and make the next
        action land somewhere unexpected."""
        s = _state(n=5)
        s.cursor = 4
        remove(s, [4])
        assert s.cursor == 3

    def test_removing_everything_leaves_it_empty(self):
        s = _state()
        remove(s, [0, 1, 2])
        assert is_empty(s)

    def test_a_populated_chooser_is_not_empty(self):
        """Pinned so `is_empty` cannot become a constant."""
        assert not is_empty(_state())


class TestItRefusesToOpenOnNothing:
    def test_an_empty_chooser_is_a_caller_bug(self):
        """A surface that opens one shows an empty box with no way out, which
        reads as a hang."""
        with pytest.raises(ValueError, match="at least one choice"):
            ChooserState(choices=[])


class TestRendering:
    def test_the_highlighted_row_is_pointed_at(self):
        """The pointer is a COLUMN in the grid, so exactly one row carries it
        and pointing at a row does not shift it sideways."""
        lines = render_lines(_state())
        pointed = [ln for ln in lines if "›" in ln]
        assert len(pointed) == 1
        assert "row 0" in pointed[0]

    def test_it_is_drawn_as_the_standard_bordered_grid(self):
        """Not picker-specific formatting: a selection overlay is a list of
        things, and this codebase has one way to draw a list of things."""
        lines = render_lines(_state())
        assert any(ln.lstrip().startswith("┌") for ln in lines)
        assert any(ln.lstrip().startswith("└") for ln in lines)
        assert any("│" in ln for ln in lines)

    def test_headers_are_drawn_only_when_given(self):
        """A header row of empty strings is worse than none — it is a band of
        nothing above the data."""
        plain = render_lines(_state())
        assert not any("priority" in ln for ln in plain)
        titled = ChooserState(
            choices=[Choice(cells=("high", "something happened"), value=1)],
            headers=("priority", "what"),
        )
        assert any("priority" in ln for ln in render_lines(titled))

    def test_a_row_with_fewer_cells_is_padded_not_dropped(self):
        """Ragged input must not shift a neighbour's column."""
        s = ChooserState(
            choices=[
                Choice(cells=("a", "b", "c"), value=1),
                Choice(cells=("a",), value=2),
            ]
        )
        lines = render_lines(s)
        assert len([ln for ln in lines if "│" in ln and "─" not in ln]) >= 2

    def test_checkboxes_appear_only_when_several_may_be_chosen(self):
        assert not any("[ ]" in ln for ln in render_lines(_state()))
        assert any("[ ]" in ln for ln in render_lines(_state(mode=ChooseMode.MANY)))

    def test_the_detail_shown_is_the_highlighted_rows(self):
        s = _state()
        move(s, 2)
        assert any("detail 2" in ln for ln in render_lines(s))
        assert not any("detail 0" in ln for ln in render_lines(s))

    def test_a_long_label_is_truncated_rather_than_wrapped(self):
        s = ChooserState(choices=[Choice(label="x" * 200, value=1)])
        assert all(len(ln) <= 80 for ln in render_lines(s, width=72))

    def test_the_key_legend_matches_the_mode(self):
        assert "Space" not in hint(ChooseMode.ONE)
        assert "Space" in hint(ChooseMode.MANY)
        assert "Esc" in hint(ChooseMode.ONE)


class TestDestructiveActionsNeedAnExplicitChoice:
    """`selection` and `checked_selection` differ exactly where it matters.

    "Enter with nothing checked means this one" is right for acknowledging an
    alert and dangerous for archiving a session: the user who pressed Enter
    having checked nothing was not aiming at whichever row their cursor
    happened to rest on.
    """

    def test_checked_selection_is_empty_when_nothing_is_checked(self):
        from axiom.extensions.builtins.chat.chooser import checked_selection

        s = _state(mode=ChooseMode.MANY)
        move(s, 1)
        assert checked_selection(s) == []
        assert selection(s) == [1], "the permissive reading still exists"

    def test_checked_selection_returns_exactly_what_was_checked(self):
        from axiom.extensions.builtins.chat.chooser import checked_selection

        s = _state(n=4, mode=ChooseMode.MANY)
        toggle(s)
        move(s, 3)
        toggle(s)
        assert checked_selection(s) == [0, 3]

    def test_it_ignores_where_the_cursor_is(self):
        from axiom.extensions.builtins.chat.chooser import checked_selection

        s = _state(mode=ChooseMode.MANY)
        toggle(s)
        move(s, 2)
        assert checked_selection(s) == [0]


class TestItNeverStrandsTheSurface:
    """The table refuses a layout it cannot draw honestly, which is right — but
    a chooser that raises takes the whole overlay down and leaves the user with
    a keypress that did nothing."""

    def test_an_impossible_width_falls_back_instead_of_raising(self):
        s = ChooserState(
            choices=[
                Choice(cells=("aaaaaaaaaa", "bbbbbbbbbb", "cccccccccc", "dddddddddd"), value=1)
            ],
            headers=("one", "two", "three", "four"),
        )
        lines = render_lines(s, width=20)  # far too narrow for four columns
        assert lines, "a chooser must always render something"
        assert any("aaaaaaaaaa" in ln for ln in lines)

    def test_the_fallback_still_shows_the_cursor(self):
        s = ChooserState(
            choices=[
                Choice(cells=("aaaaaaaaaa", "bbbbbbbbbb", "cccccccccc", "dddddddddd"), value=1),
                Choice(cells=("eeeeeeeeee", "ffffffffff", "gggggggggg", "hhhhhhhhhh"), value=2),
            ],
        )
        move(s, 1)
        lines = render_lines(s, width=20)
        pointed = [ln for ln in lines if "›" in ln]
        assert len(pointed) == 1
        assert "eeeeeeeeee" in pointed[0]

    def test_a_workable_width_still_draws_the_grid(self):
        """Pinned so the fallback cannot become the only path and quietly
        replace the standard rendering everywhere."""
        assert any(ln.lstrip().startswith("┌") for ln in render_lines(_state(), width=72))
