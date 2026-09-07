# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Headless probes for the fullscreen TUI, one per open defect (D1-D10).

Each test states the behaviour the TUI *should* have. The ones marked
``xfail(strict=True)`` reproduce a defect from the 2026-09-03 register. When a
defect is fixed the test XPASSes, strict mode fails the run, and the marker
comes off in the same PR. The marker *is* the defect's open state, so it
cannot be forgotten.

Register: https://claude.ai/code/artifact/dab83a50-0d6c-4499-8500-62b11cc154ff
Harness:  ``axiom_tests.fixtures.tui`` (headless terminal + key-binding lint).
"""

from __future__ import annotations

import asyncio
import subprocess
import time
from types import SimpleNamespace

import pytest
from axiom_tests.fixtures.tui import find_binding_conflicts, key_sequence
from prompt_toolkit.application.current import set_app
from prompt_toolkit.data_structures import Point
from prompt_toolkit.document import Document
from prompt_toolkit.formatted_text.utils import fragment_list_to_text, fragment_list_width
from prompt_toolkit.layout.mouse_handlers import MouseHandlers
from prompt_toolkit.layout.screen import Screen, WritePosition
from prompt_toolkit.mouse_events import MouseButton, MouseEvent, MouseEventType

from axiom.extensions.builtins.chat.clipboard import SystemClipboard
from axiom.extensions.builtins.chat.fullscreen import FullScreenChat, _OutputLexer

# ---------------------------------------------------------------------------
# Rig
# ---------------------------------------------------------------------------


def _build_tui(factory, *, columns: int = 80, rows: int = 24):
    """A real FullScreenChat on a headless terminal of a fixed size."""
    agent = SimpleNamespace(
        session=SimpleNamespace(id="probe", messages=[], title="probe"),
        cancel=lambda: None,
        allowlisted_tools=lambda: [],
    )
    store = SimpleNamespace(list_sessions=lambda **kw: [], save=lambda *a, **k: None)
    tui = FullScreenChat(agent=agent, store=store, stream=True)
    app = tui._build_app()
    term = factory(app, columns=columns, rows=rows)
    tui._app = app
    return tui, term


def _picker_states(tui) -> dict:
    """The two UI states that change which bindings are live."""
    return {
        "picker closed": lambda: setattr(tui, "_picker", None),
        "picker open": lambda: setattr(tui, "_picker", object()),
    }


def _handler(tui, keys: tuple[str, ...], *, picker_open: bool = False):
    """The handler prompt_toolkit would run for *keys* in the given picker state."""
    tui._picker = object() if picker_open else None
    match = None
    for b in tui._app.key_bindings.bindings:
        if key_sequence(b) != keys:
            continue
        try:
            live = bool(b.filter())
        except Exception:  # noqa: BLE001 - needs a live app; treat as active
            live = True
        if live:
            match = b.handler  # last registered wins, as in prompt_toolkit
    tui._picker = None
    assert match is not None, f"no live binding for {keys}"
    return match


def _event(tui, *, clipboard=None):
    """Enough of a KeyPressEvent for the handlers under test.

    The default clipboard is an inert stub so copy paths never touch the
    real OS clipboard; pass one to test the paste path.
    """
    if clipboard is None:
        clipboard = SimpleNamespace(set_data=lambda data: None)
    return SimpleNamespace(
        app=SimpleNamespace(
            clipboard=clipboard,
            invalidate=lambda: None,
            layout=tui._app.layout,
        ),
        current_buffer=tui._input_buffer,
    )


def _wheel(event_type: MouseEventType) -> MouseEvent:
    """A mouse-wheel tick over the output pane."""
    return MouseEvent(
        position=Point(x=0, y=0),
        event_type=event_type,
        button=MouseButton.NONE,
        modifiers=frozenset(),
    )


def _hundred_lines(tui, *, at_end: bool = False) -> None:
    """A 100-line output document, cursor at the top or (following) at the text's end."""
    text = "\n".join(f"line {i}" for i in range(100))
    cursor = len(text) if at_end else 0
    tui._output_buffer.set_document(Document(text, cursor), bypass_readonly=True)


def _render(tui, term) -> list[str]:
    """One real frame of the layout on the headless terminal, as stripped rows.

    Goes through ``write_to_screen`` so every render-time side effect happens:
    prompt_toolkit's scroll clamp, wrapping, and ``_ScrollMetricsCapture``
    recording the window height. Needs a loop for the buffers' history load.
    """

    async def frame() -> Screen:
        tui._app.loop = asyncio.get_running_loop()
        screen = Screen()
        with set_app(tui._app):
            tui._app.layout.container.write_to_screen(
                screen,
                MouseHandlers(),
                WritePosition(0, 0, term.columns, term.rows),
                "",
                True,
                None,
            )
        return screen

    screen = asyncio.run(frame())
    rows = []
    for y in range(term.rows):
        cells = screen.data_buffer.get(y, {})
        rows.append(
            "".join(cells[x].char if x in cells else " " for x in range(term.columns)).rstrip()
        )
    return rows


@pytest.fixture
def tui_factory(headless_terminal_factory):
    def _make(**kw):
        return _build_tui(headless_terminal_factory, **kw)

    return _make


# ---------------------------------------------------------------------------
# Rig sanity: the harness itself works against the real TUI
# ---------------------------------------------------------------------------


def test_headless_tui_builds_and_wraps(tui_factory):
    tui, term = tui_factory(columns=80, rows=24)
    assert term.app.output.get_size().columns == 80
    assert 0 < tui._get_wrap_width() <= 80
    tui._append_output("hello\n")
    assert "hello" in tui._output_buffer.text


# ---------------------------------------------------------------------------
# P1 defects: demo blockers
# ---------------------------------------------------------------------------


def test_d1_ctrl_c_cancels_busy_turn_despite_stale_selection(tui_factory):
    """Fixed: a running turn is interrupted before any copy path is considered."""
    tui, _ = tui_factory()
    cancelled: list[bool] = []
    tui._agent.cancel = lambda: cancelled.append(True)
    tui._busy = True

    # The state any completed mouse drag leaves behind.
    ctrl = tui._output_window.content
    ctrl.sel_start, ctrl.sel_end = 0, 5
    tui._output_buffer.set_document(Document("hello world", 0), bypass_readonly=True)

    _handler(tui, ("c-c",))(_event(tui))

    assert cancelled, "ctrl+C must interrupt a busy turn, selection or not"


def test_ctrl_c_on_idle_tui_copies_output_selection_and_clears_it(tui_factory):
    """The copy path survives the D1 fix: idle + selection means copy, not cancel."""
    tui, _ = tui_factory()
    cancelled: list[bool] = []
    tui._agent.cancel = lambda: cancelled.append(True)
    tui._busy = False
    copied: list[str] = []
    tui._output_buffer.set_document(Document("hello world", 0), bypass_readonly=True)
    ctrl = tui._output_window.content
    ctrl.sel_start, ctrl.sel_end = 0, 5

    event = _event(tui)
    event.app.clipboard.set_data = lambda data: copied.append(data.text)
    _handler(tui, ("c-c",))(event)

    assert copied == ["hello"]
    assert not cancelled
    assert ctrl.sel_start is None and ctrl.sel_end is None


def test_d2_table_rows_visible_while_streaming(tui_factory):
    """Fixed: pending table rows are shown provisionally while the table streams."""
    tui, _ = tui_factory()
    for chunk in (
        "Here is the rod worth summary:\n",
        "| Rod | Worth |\n",
        "|-----|-------|\n",
        "| A   | 2.1$  |\n",
    ):
        tui._append_output(chunk)

    visible = tui._output_buffer.text
    assert "| Rod" in visible and "| A" in visible, (
        f"{len(tui._table_pending)} row(s) stuck in _table_pending; "
        f"buffer tail={visible.splitlines()[-1] if visible.strip() else '<empty>'!r}"
    )


def test_streamed_table_matches_rewrap_once_complete(tui_factory):
    """Streaming a table chunk by chunk ends in the same text a full rewrap produces.

    The provisional rendering must not commit rows early: the table is still
    aligned as one block when a non-table line closes it.
    """
    tui, _ = tui_factory()
    chunks = [
        "Rod worths:\n",
        "| Rod | Worth |\n",
        "|---|---|\n",
        "| A | 2.1$ |\n",
        "| Longer name | 0.35$ |\n",
        "Done.\n",
    ]
    for i, c in enumerate(chunks):
        tui._append_output(c)
        if i >= 1:  # header row has streamed; it must be visible from now on
            assert "| Rod" in tui._output_buffer.text
    streamed = tui._output_buffer.text
    assert tui._table_pending == []  # closed by the trailing non-table line

    tui._rewrap_buffer()
    assert tui._output_buffer.text == streamed


def test_d3_render_pass_after_resize_reflows_buffer(tui_factory):
    """Fixed: ``_rewrap_if_resized`` on ``app.before_render`` reflows at the new width.

    prompt_toolkit redraws on every resize, so hooking the render path covers
    SIGWINCH and its own size check alike.
    """
    tui, term = tui_factory(columns=80)
    tui._append_output(
        "The reactor control rod calibration procedure requires that each rod be "
        "measured independently against the calibrated reference position before "
        "any reactivity insertion occurs.\n"
    )
    narrow = tui._output_buffer.text
    assert max(len(line) for line in narrow.splitlines()) <= 80

    term.resize(200, 24)
    tui._app.before_render.fire()

    assert tui._output_buffer.text != narrow, "buffer still wrapped at the old width"
    assert max(len(line) for line in tui._output_buffer.text.splitlines()) > 80


def test_render_pass_without_resize_does_not_rewrap(tui_factory):
    """The resize hook is a comparison on the hot path, not a rewrap per frame."""
    tui, _ = tui_factory(columns=80)
    tui._append_output("a line\n")
    calls: list[int] = []
    original = tui._rewrap_buffer
    tui._rewrap_buffer = lambda **kw: (calls.append(1), original(**kw))

    tui._app.before_render.fire()
    tui._app.before_render.fire()
    assert calls == []

    tui._app.output.get_size = lambda: __import__(
        "prompt_toolkit.data_structures", fromlist=["Size"]
    ).Size(rows=24, columns=120)
    tui._app.before_render.fire()
    assert calls == [1]


def test_d5_last_chunk_of_a_burst_gets_painted(tui_factory):
    """Fixed: a chunk inside the throttle window arms a trailing-edge repaint.

    Headless, the app has no running loop, so the repaint is issued directly;
    the timer path is covered by the next test.
    """
    tui, _ = tui_factory()
    painted: list[int] = []
    tui._app.invalidate = lambda: painted.append(1)

    tui._last_invalidate = time.monotonic()  # inside a throttle window
    tui._append_output("first ")
    tui._append_output("second")

    has_timer = bool(getattr(tui._app, "refresh_interval", None))
    assert painted or has_timer, "no trailing-edge repaint and no periodic refresh"


def test_d5_trailing_repaint_fires_once_when_the_window_closes(tui_factory):
    """With a live loop: chunks inside the window paint exactly once, at the window's end."""
    import asyncio

    tui, _ = tui_factory()
    painted: list[float] = []
    tui._app.invalidate = lambda: painted.append(time.monotonic())
    loop = asyncio.new_event_loop()
    try:
        tui._app.loop = loop
        t0 = time.monotonic()
        tui._last_invalidate = t0  # a paint just happened
        for chunk in ("a", "b", "c"):
            tui._append_output(chunk)
        assert painted == []  # inside the window: deferred, not dropped
        assert tui._pending_invalidate is True

        loop.run_until_complete(asyncio.sleep(0.12))
        assert len(painted) == 1
        assert painted[0] - t0 >= 0.04
        assert tui._pending_invalidate is False
    finally:
        loop.close()


# ---------------------------------------------------------------------------
# P2 defects: degrade with session length
# ---------------------------------------------------------------------------


def test_d4_lexer_cost_is_bounded_by_visible_window():
    """D4 fixed: ``lex_document`` is O(1). It returns a lazy ``get_line`` and
    styles nothing up front, so handing the lexer an 8000-line scrollback costs
    the same as a 500-line one; the renderer pays only for the rows it paints."""
    lexer = _OutputLexer(None)

    def cost_ms(n_lines: int) -> float:
        body = "\n".join(
            f"Line {i}: rod worth measured at {i / 10:.2f} dollars during calibration."
            for i in range(n_lines)
        )
        doc = Document(body, 0)
        best = float("inf")
        for _ in range(3):
            t0 = time.perf_counter()
            lexer.lex_document(doc)
            best = min(best, (time.perf_counter() - t0) * 1000)
        return best

    small, large = cost_ms(500), cost_ms(8000)
    growth = large / max(small, 1e-6)
    assert growth < 4, f"lex_document: 500 lines={small:.1f}ms, 8000={large:.1f}ms ({growth:.0f}x)"


def test_d4_visible_window_cost_amortises_the_fence_scan():
    """Styling the last 20 lines costs about the same at 8000 lines as at 500,
    even with a code fence near the top: the fence scan runs once per document
    (marker to marker, not line by line) and is shared by every ``get_line``
    call. The hash tracks the text, so a changed document never reuses a stale
    ``get_line``."""
    lexer = _OutputLexer(None)
    prose = "Line {}: steady state at {:.2f} dollars."

    def doc_with_fence(n_lines: int) -> Document:
        head = [
            "Rod worth calibration.",
            "```python",
            "def rod_worth(x):",
            "    return x * 2",
            "```",
        ]
        body = head + [prose.format(i, i / 10) for i in range(n_lines - len(head))]
        return Document("\n".join(body), 0)

    def window_cost_ms(doc: Document) -> float:
        last = doc.line_count
        best = float("inf")
        for _ in range(3):
            t0 = time.perf_counter()
            get_line = lexer.lex_document(doc)
            for i in range(last - 20, last):
                get_line(i)
            best = min(best, (time.perf_counter() - t0) * 1000)
        return best

    small_doc, large_doc = doc_with_fence(500), doc_with_fence(8000)
    small, large = window_cost_ms(small_doc), window_cost_ms(large_doc)
    growth = large / max(small, 1e-6)
    assert growth < 10, (
        f"last 20 lines: 500 lines={small:.2f}ms, 8000={large:.2f}ms ({growth:.0f}x)"
    )

    # The lazy scan still knows where the fence is: code inside it is
    # Pygments-styled and the first line after it is plain prose.
    get_line = lexer.lex_document(large_doc)
    assert any("pygments" in style for style, _ in get_line(2))
    after_fence = get_line(5)
    assert all("pygments" not in style and "dim" not in style for style, _ in after_fence)
    assert "".join(text for _, text in after_fence) == prose.format(0, 0.0)

    lexer.lex_document(small_doc)
    h_small = lexer.invalidation_hash()
    lexer.lex_document(large_doc)
    assert lexer.invalidation_hash() != h_small


def test_d6_scroll_stops_with_the_window_still_full(tui_factory):
    """Fixed: both scroll paths clamp to ``_max_scroll_row`` (line_count - window_height).

    The window height is what ``_ScrollMetricsCapture`` records on each render;
    the keyboard path also sets the window's ``vertical_scroll`` so the target
    row is the top of the pane, not merely somewhere inside it.
    """
    tui, _ = tui_factory(rows=24)
    _hundred_lines(tui)
    tui._scroll_window_height = 20  # what _ScrollMetricsCapture records after a render

    page_down = _handler(tui, ("pagedown",))
    for _ in range(10):
        page_down(_event(tui))

    row = tui._output_buffer.document.cursor_position_row
    assert row <= 100 - 20, f"scrolled to row {row}; last useful position is 80"
    assert tui._output_window.vertical_scroll == row


def test_d6_guard_before_first_render_the_clamp_is_the_last_line(tui_factory):
    """With no window height captured yet (0), the only safe bound is the last line."""
    tui, _ = tui_factory(rows=24)
    _hundred_lines(tui)
    assert tui._scroll_window_height == 0

    page_down = _handler(tui, ("pagedown",))
    for _ in range(10):
        page_down(_event(tui))

    assert tui._output_buffer.document.cursor_position_row == 99


def test_d6_guard_page_up_from_the_bottom_moves_a_full_page(tui_factory):
    """From the bottom, Page Up shows the previous page.

    While following, the cursor is at the text's end (row 99) but the scroll
    position is the last full window (row 80), so the first Page Up moves 20
    rows, not one.
    """
    tui, _ = tui_factory(rows=24)
    _hundred_lines(tui, at_end=True)
    tui._scroll_window_height = 20
    page_up = _handler(tui, ("pageup",))

    page_up(_event(tui))
    assert tui._output_buffer.document.cursor_position_row == 60
    assert tui._output_window.vertical_scroll == 60
    page_up(_event(tui))
    assert tui._output_buffer.document.cursor_position_row == 40


def test_d6_guard_mouse_wheel_shares_the_clamp(tui_factory):
    """The wheel path stops at the same row as the keys, so the pane stays full either way."""
    tui, _ = tui_factory(rows=24)
    _hundred_lines(tui)
    tui._scroll_window_height = 20
    ctrl = tui._output_window.content

    for _ in range(50):  # each tick is 3-12 rows: far past the end
        ctrl.mouse_handler(_wheel(MouseEventType.SCROLL_DOWN))
    assert tui._output_window.vertical_scroll == 80
    assert tui._output_buffer.document.cursor_position_row == 80

    ctrl.mouse_handler(_wheel(MouseEventType.SCROLL_UP))
    assert tui._output_window.vertical_scroll < 80


def test_d6_guard_rendered_pane_is_full_at_the_bottom(tui_factory):
    """The defect as seen: after paging past the end, the pane still shows a full window.

    Renders for real, so the window height is the captured one and prompt_toolkit's
    own scroll pass runs. The last row is the empty line after the final newline;
    every row above it is text.
    """
    tui, term = tui_factory(columns=80, rows=24)
    tui._append_output("".join(f"line {i}\n" for i in range(100)))
    _render(tui, term)
    height = tui._scroll_window_height
    assert height > 0

    page_down = _handler(tui, ("pagedown",))
    for _ in range(10):
        page_down(_event(tui))
        rows = _render(tui, term)

    assert tui._output_buffer.document.cursor_position_row == 101 - height
    assert rows[height - 2] == "line 99" and rows[height - 1] == ""
    assert all(rows[y] for y in range(height - 1)), rows[:height]


def test_d6_guard_scrolling_to_the_bottom_resumes_following(tui_factory):
    """The last full window counts as the bottom, as the last line did before.

    Scrolled up, appended output leaves the view alone and the status line shows
    a scroll position. Paged back down to the last full window (cursor short of
    the text's end), the hint clears and the next append follows again.
    """
    tui, _ = tui_factory(rows=24)
    tui._append_output("".join(f"line {i}\n" for i in range(100)))
    tui._scroll_window_height = 20
    page_up, page_down = _handler(tui, ("pageup",)), _handler(tui, ("pagedown",))

    page_up(_event(tui))
    row = tui._output_buffer.document.cursor_position_row
    tui._append_output("line 100\n")
    assert tui._output_buffer.document.cursor_position_row == row
    assert "\u2191" in fragment_list_to_text(tui._get_status_text())

    for _ in range(10):
        page_down(_event(tui))
    assert tui._output_buffer.cursor_position < len(tui._output_buffer.text)
    assert "\u2191" not in fragment_list_to_text(tui._get_status_text())
    tui._append_output("line 101\n")
    assert tui._output_buffer.cursor_position == len(tui._output_buffer.text)


# ---------------------------------------------------------------------------
# P3 defects: key table and clipboard
# ---------------------------------------------------------------------------


def test_d7_d8_key_table_has_no_shadowed_or_ambiguous_bindings(tui_factory):
    """Fixed: no key is bound twice in one picker state, and no single key is a chord prefix."""
    tui, _ = tui_factory()
    conflicts = find_binding_conflicts(tui._app.key_bindings, _picker_states(tui))
    assert conflicts == [], "\n".join(c.describe() for c in conflicts)


def _load_output(tui, n_lines: int = 100) -> None:
    tui._output_buffer.set_document(
        Document("\n".join(f"line {i}" for i in range(n_lines)), 0), bypass_readonly=True
    )


@pytest.mark.parametrize(
    ("down_keys", "up_keys"),
    [(("c-down",), ("c-up",)), (("escape", "down"), ("escape", "up"))],
    ids=["ctrl+arrows", "option+arrows"],
)
def test_d7_scroll_keys_move_the_output_cursor(tui_factory, down_keys, up_keys):
    """Ctrl+Up/Down and Option+Up/Down scroll the output three lines through the real handlers."""
    tui, _ = tui_factory()
    _load_output(tui)
    doc = lambda: tui._output_buffer.document  # noqa: E731

    _handler(tui, down_keys)(_event(tui))
    assert doc().cursor_position_row == 3
    _handler(tui, down_keys)(_event(tui))
    assert doc().cursor_position_row == 6
    _handler(tui, up_keys)(_event(tui))
    assert doc().cursor_position_row == 3
    assert tui._input_buffer.selection_state is None, "scrolling must not touch the input"


def test_d7_scroll_keys_are_inert_while_the_picker_is_open(tui_factory):
    """The scroll chords carry the same no-picker filter as Page Up/Down."""
    tui, _ = tui_factory()
    tui._picker = object()
    try:
        live = [
            key_sequence(b)
            for b in tui._app.key_bindings.bindings
            if key_sequence(b) in {("c-up",), ("c-down",), ("escape", "up"), ("escape", "down")}
            and b.filter()
        ]
    finally:
        tui._picker = None
    assert live == []


def test_d7_shift_up_and_down_extend_the_input_selection(tui_factory):
    """Fixed: shift+arrows select in the input again instead of scrolling the output."""
    tui, _ = tui_factory()
    _load_output(tui)
    buf = tui._input_buffer
    text = "first line\nsecond line"
    buf.set_document(Document(text, len(text)), bypass_readonly=False)

    _handler(tui, ("s-up",))(_event(tui))
    assert buf.selection_state is not None
    assert buf.selection_state.original_cursor_position == len(text)
    assert buf.document.cursor_position_row == 0
    assert tui._output_buffer.document.cursor_position_row == 0, "output must not scroll"

    _handler(tui, ("s-up",))(_event(tui))  # already on the first line: select to start
    assert buf.cursor_position == 0

    buf.selection_state = None
    buf.cursor_position = 0
    _handler(tui, ("s-down",))(_event(tui))
    assert buf.selection_state is not None
    assert buf.document.cursor_position_row == 1
    _handler(tui, ("s-down",))(_event(tui))  # already on the last line: select to end
    assert buf.cursor_position == len(text)


def test_d8_ctrl_a_goes_to_line_start_and_is_not_a_chord_prefix(tui_factory):
    """Fixed: Ctrl+A moves immediately; no longer sequence starts with it."""
    tui, _ = tui_factory()
    buf = tui._input_buffer
    text = "first line\nsecond line"
    buf.set_document(Document(text, len(text)), bypass_readonly=False)

    _handler(tui, ("c-a",))(_event(tui))
    assert buf.cursor_position == len("first line\n")
    assert buf.selection_state is None

    chords = [key_sequence(b) for b in tui._app.key_bindings.bindings]
    assert [k for k in chords if len(k) > 1 and k[0] == "c-a"] == []
    conflicts = find_binding_conflicts(tui._app.key_bindings, _picker_states(tui))
    assert [c for c in conflicts if c.keys == ("c-a",)] == []


def test_key_lint_ignores_picker_disambiguated_pairs(tui_factory):
    """Enter/Up/Down have a picker and a no-picker binding each; that is by design."""
    tui, _ = tui_factory()
    conflicts = find_binding_conflicts(tui._app.key_bindings, _picker_states(tui))
    flagged = {c.keys for c in conflicts}
    assert ("enter",) not in flagged and ("up",) not in flagged and ("down",) not in flagged


def test_d9_text_column_and_chrome_agree_on_width(tui_factory):
    """Fixed: text, border rules and status line are all sized from ``_content_width``.

    The chrome is ``_CHROME_WIDTH`` = 4 cells: 2 scrollbar, 1 gutter, and 1 wrap
    margin. The margin is not decorative: BufferControl keeps one cell after
    every line for the cursor, and with wrap_lines a line that exactly fills
    the window pushes that cell onto a blank continuation row (see the guard
    below). So the text keeps its columns-4, and the rules, which used to be
    drawn at columns-3, now match it instead.
    """
    tui, term = tui_factory(columns=80)
    assert tui._get_wrap_width() == term.columns - 4


def test_d9_guard_border_rule_and_status_line_span_the_wrap_width(tui_factory):
    """The frame is exactly as wide as the text it frames, at any terminal width."""
    tui, term = tui_factory(columns=100)
    for columns in (100, 60):
        term.resize(columns, 24)
        width = tui._get_wrap_width()
        assert width == columns - 4
        assert fragment_list_width(tui._get_border_text()) == width
        assert fragment_list_width(tui._get_status_text()) == width


def test_d9_guard_a_wrap_width_line_takes_one_screen_row(tui_factory):
    """Why the wrap margin exists: a wrap-width line must not spill onto a blank row.

    One cell wider and it would: BufferControl's cursor cell wraps on its own
    when a line fills the window (D6's row-for-row clamp relies on this too).
    """
    tui, term = tui_factory(columns=80, rows=24)
    width = tui._get_wrap_width()
    tui._append_output("X" * width + "\nY\n")

    rows = _render(tui, term)

    assert rows[0] == "X" * width
    assert rows[1] == "Y"
    assert tui._scroll_window_width == width + 1  # the margin is the window's last cell


def test_d10_paste_reads_the_system_clipboard(tui_factory):
    """Fixed: the Application is built with clipboard=SystemClipboard.

    get_data asks the OS first (pbpaste/xclip/xsel) and falls back to the
    in-memory ring, so ctrl+V sees text copied outside the app.
    """
    tui, _ = tui_factory()
    assert type(tui._app.clipboard).__name__ != "InMemoryClipboard"
    assert isinstance(tui._app.clipboard, SystemClipboard)
    assert tui._app.clipboard is tui._clipboard


def test_d10_ctrl_v_inserts_what_the_system_clipboard_holds(tui_factory):
    """ctrl+V goes through app.clipboard, so a paste tool's output lands in the input."""
    tui, _ = tui_factory()

    def pbpaste(cmd, data, timeout):
        return subprocess.CompletedProcess(cmd, 0, stdout=b"copied outside the app")

    clipboard = SystemClipboard(runner=pbpaste, platform="darwin")
    tui._input_buffer.set_document(Document("say: ", 5), bypass_readonly=False)

    _handler(tui, ("c-v",))(_event(tui, clipboard=clipboard))

    assert tui._input_buffer.text == "say: copied outside the app"
