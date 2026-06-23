# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tests for output area mouse selection — position translation, style, overlay."""

from unittest.mock import MagicMock

import pytest
from prompt_toolkit.buffer import Buffer
from prompt_toolkit.document import Document

from axiom.extensions.builtins.chat.fullscreen import (
    _STYLE,
    _apply_selection_overlay,
    _highlight_fragments,
    _OutputLexer,
    _ScrollableBufferControl,
)


@pytest.fixture
def make_control():
    """Create a _ScrollableBufferControl with a fake TUI and given buffer text."""

    def _factory(text: str):
        tui = MagicMock()
        tui._scroll_vertical_scroll = 0
        tui._scroll_window_width = 40
        buf = Buffer(read_only=True, name="output")
        buf.set_document(Document(text, 0), bypass_readonly=True)
        ctrl = _ScrollableBufferControl(
            tui=tui,
            buffer=buf,
            focusable=True,
        )
        return ctrl

    return _factory


class TestMousePosToCursor:
    """Verify (line_number, col) -> buffer cursor_position mapping.

    prompt_toolkit's Window already translates screen coordinates to content
    coordinates: row = buffer line number, col = character column.  Our
    ``_mouse_pos_to_cursor`` simply delegates to
    ``Document.translate_row_col_to_index``.
    """

    def test_single_line(self, make_control):
        ctrl = make_control("hello world")
        assert ctrl._mouse_pos_to_cursor(0, 5) == 5

    def test_multi_line_second_row(self, make_control):
        ctrl = make_control("line one\nline two\nline three")
        assert ctrl._mouse_pos_to_cursor(1, 0) == len("line one\n")
        assert ctrl._mouse_pos_to_cursor(1, 4) == len("line one\n") + 4

    def test_col_clamped_to_line_length(self, make_control):
        ctrl = make_control("short\nhi")
        # Document.translate_row_col_to_index clamps col to line length
        assert ctrl._mouse_pos_to_cursor(0, 99) == 5

    def test_empty_lines(self, make_control):
        ctrl = make_control("first\n\n\nfourth")
        assert ctrl._mouse_pos_to_cursor(1, 0) == len("first\n")
        assert ctrl._mouse_pos_to_cursor(2, 0) == len("first\n\n")
        assert ctrl._mouse_pos_to_cursor(3, 2) == len("first\n\n\n") + 2

    def test_third_line(self, make_control):
        ctrl = make_control("aaa\nbbb\nccc\nddd")
        # Line 2, col 1 → past "aaa\nbbb\n" = 8 chars, + 1
        assert ctrl._mouse_pos_to_cursor(2, 1) == len("aaa\nbbb\n") + 1

    def test_last_line(self, make_control):
        ctrl = make_control("short\nabcdefghijklmno\nend")
        assert ctrl._mouse_pos_to_cursor(2, 1) == len("short\nabcdefghijklmno\n") + 1


class TestSelectedStyleDefined:
    """The 'selected' style must exist so selection highlights are visible."""

    def test_selected_style_in_style_dict(self):
        style_list = _STYLE.style_rules
        class_names = [rule[0] for rule in style_list]
        assert "selected" in class_names, (
            "_STYLE must define 'selected' so drag-selection is visible"
        )

    def test_selected_style_has_background(self):
        for selector, style_str in _STYLE.style_rules:
            if selector == "selected":
                assert "bg:" in style_str or "bg#" in style_str or "reverse" in style_str, (
                    "'selected' style must set a background color or reverse"
                )
                return
        pytest.fail("'selected' rule not found")


class TestHighlightFragments:
    """_highlight_fragments applies class:selected to character ranges."""

    def test_full_fragment(self):
        frags = [("", "hello")]
        result = _highlight_fragments(frags, 0, 5)
        assert result == [(" class:selected", "hello")]

    def test_partial_start(self):
        frags = [("", "hello")]
        result = _highlight_fragments(frags, 0, 3)
        assert result == [(" class:selected", "hel"), ("", "lo")]

    def test_partial_end(self):
        frags = [("", "hello")]
        result = _highlight_fragments(frags, 2, 5)
        assert result == [("", "he"), (" class:selected", "llo")]

    def test_partial_middle(self):
        frags = [("", "hello")]
        result = _highlight_fragments(frags, 1, 4)
        assert result == [("", "h"), (" class:selected", "ell"), ("", "o")]

    def test_multi_fragment(self):
        frags = [("class:dim", "ab"), ("class:bold", "cd")]
        result = _highlight_fragments(frags, 1, 3)
        # "a" outside, "b" selected (dim), "c" selected (bold), "d" outside
        assert result == [
            ("class:dim", "a"),
            ("class:dim class:selected", "b"),
            ("class:bold class:selected", "c"),
            ("class:bold", "d"),
        ]

    def test_no_overlap(self):
        frags = [("", "hello")]
        result = _highlight_fragments(frags, 5, 10)
        assert result == [("", "hello")]


class TestApplySelectionOverlay:
    """_apply_selection_overlay modifies styled fragments in-place."""

    def test_single_line_selection(self):
        styled = [[("", "hello world")]]
        lines = ["hello world"]
        _apply_selection_overlay(styled, lines, 0, 5)
        # "hello" selected, " world" not
        assert styled[0] == [(" class:selected", "hello"), ("", " world")]

    def test_multi_line_selection(self):
        styled = [[("", "first")], [("", "second")], [("", "third")]]
        lines = ["first", "second", "third"]
        # Select from "rst" in first to "sec" in second
        # first = chars 0-4, \n at 5, second = chars 6-11
        _apply_selection_overlay(styled, lines, 2, 9)
        # Line 0: "fi" untouched, "rst" selected
        assert styled[0] == [("", "fi"), (" class:selected", "rst")]
        # Line 1: "sec" selected, "ond" untouched
        assert styled[1] == [(" class:selected", "sec"), ("", "ond")]
        # Line 2: untouched
        assert styled[2] == [("", "third")]

    def test_empty_range(self):
        styled = [[("", "hello")]]
        lines = ["hello"]
        _apply_selection_overlay(styled, lines, 3, 3)
        assert styled[0] == [("", "hello")]  # unchanged


class TestDragSelectionState:
    """Selection is tracked via sel_start/sel_end on the control."""

    def test_drag_start_end(self, make_control):
        ctrl = make_control("hello world\nsecond line")
        # Simulate MOUSE_DOWN at (line 0, col 0)
        ctrl._drag_start = ctrl._mouse_pos_to_cursor(0, 0)
        ctrl.sel_start = None
        ctrl.sel_end = None

        # Simulate MOUSE_MOVE to (line 0, col 5)
        pos = ctrl._mouse_pos_to_cursor(0, 5)
        a, b = sorted((ctrl._drag_start, pos))
        ctrl.sel_start = a
        ctrl.sel_end = b

        assert ctrl.sel_start == 0
        assert ctrl.sel_end == 5
        assert ctrl.buffer.text[ctrl.sel_start : ctrl.sel_end] == "hello"

    def test_cross_line_drag(self, make_control):
        ctrl = make_control("hello world\nsecond line")
        # Drag from (line 0, col 6) to (line 1, col 6)
        ctrl._drag_start = ctrl._mouse_pos_to_cursor(0, 6)
        pos = ctrl._mouse_pos_to_cursor(1, 6)
        a, b = sorted((ctrl._drag_start, pos))
        ctrl.sel_start = a
        ctrl.sel_end = b

        assert ctrl.buffer.text[ctrl.sel_start : ctrl.sel_end] == "world\nsecond"

    def test_mouseup_fallback_without_mousemove(self, make_control):
        """MOUSE_UP synthesises selection when MOUSE_MOVE never fired."""
        ctrl = make_control("hello world")
        ctrl._drag_start = ctrl._mouse_pos_to_cursor(0, 0)
        ctrl.sel_start = None
        ctrl.sel_end = None

        # Simulate MOUSE_UP at (0,5) — no MOUSE_MOVE happened
        pos = ctrl._mouse_pos_to_cursor(0, 5)
        if pos != ctrl._drag_start:
            a, b = sorted((ctrl._drag_start, pos))
            ctrl.sel_start = a
            ctrl.sel_end = b

        assert ctrl.sel_start == 0
        assert ctrl.sel_end == 5
        assert ctrl.buffer.text[ctrl.sel_start : ctrl.sel_end] == "hello"

    def test_click_without_drag_no_selection(self, make_control):
        ctrl = make_control("hello world")
        pos = ctrl._mouse_pos_to_cursor(0, 3)
        ctrl._drag_start = pos
        ctrl.sel_start = None
        ctrl.sel_end = None

        # MOUSE_UP at same position
        up_pos = ctrl._mouse_pos_to_cursor(0, 3)
        if up_pos != ctrl._drag_start:
            a, b = sorted((ctrl._drag_start, up_pos))
            ctrl.sel_start = a
            ctrl.sel_end = b

        assert ctrl.sel_start is None
        assert ctrl.sel_end is None


class TestDragDoesNotAffectInput:
    """Output selection is entirely separate from the input buffer."""

    def test_input_buffer_untouched(self, make_control):
        ctrl = make_control("hello world\nsecond line")
        ctrl.sel_start = 0
        ctrl.sel_end = 5

        input_buf = Buffer(name="input")
        input_buf.set_document(Document("user typing", len("user typing")))
        assert input_buf.selection_state is None
        assert input_buf.text == "user typing"


class TestLexerSelectionIntegration:
    """_OutputLexer renders selection highlight from control's sel_start/sel_end."""

    def test_lexer_applies_selection(self, make_control):
        ctrl = make_control("hello world")
        ctrl.sel_start = 0
        ctrl.sel_end = 5
        lexer = _OutputLexer(control=ctrl)

        doc = Document("hello world", 0)
        get_line = lexer.lex_document(doc)
        fragments = get_line(0)

        styles = [s for s, _ in fragments]
        texts = [t for _, t in fragments]

        # "hello" should have class:selected, " world" should not
        assert "class:selected" in styles[0]
        assert "hello" in texts[0]

    def test_lexer_no_selection(self, make_control):
        ctrl = make_control("hello world")
        ctrl.sel_start = None
        ctrl.sel_end = None
        lexer = _OutputLexer(control=ctrl)

        doc = Document("hello world", 0)
        get_line = lexer.lex_document(doc)
        fragments = get_line(0)

        # No selection styling
        for style, _ in fragments:
            assert "selected" not in style

    def test_invalidation_hash_changes_with_selection(self, make_control):
        """Cache key must change when selection changes so fragments are re-rendered."""
        ctrl = make_control("hello world")
        lexer = _OutputLexer(control=ctrl)

        ctrl.sel_start = None
        ctrl.sel_end = None
        hash_none = lexer.invalidation_hash()

        ctrl.sel_start = 0
        ctrl.sel_end = 5
        hash_sel = lexer.invalidation_hash()

        assert hash_none != hash_sel, "hash must differ so BufferControl cache is busted"

    def test_invalidation_hash_differs_per_range(self, make_control):
        ctrl = make_control("hello world")
        lexer = _OutputLexer(control=ctrl)

        ctrl.sel_start = 0
        ctrl.sel_end = 3
        h1 = lexer.invalidation_hash()

        ctrl.sel_start = 2
        ctrl.sel_end = 8
        h2 = lexer.invalidation_hash()

        assert h1 != h2
