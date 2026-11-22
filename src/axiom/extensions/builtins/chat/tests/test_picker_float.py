# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""T4.11 — picker shown as Float overlay; output buffer not mutated."""

from __future__ import annotations

import threading
from unittest.mock import MagicMock

from prompt_toolkit.buffer import Buffer
from prompt_toolkit.document import Document
from prompt_toolkit.formatted_text import FormattedText


def _make_tui():
    from axiom.extensions.builtins.chat.fullscreen import FullScreenChat

    tui = object.__new__(FullScreenChat)
    tui._output_lock = threading.Lock()
    tui._raw_output = ""
    tui._wrapped_committed = ""
    tui._partial_tail = ""
    tui._table_pending = []
    tui._align_table_cache = {}
    tui._last_invalidate = 0.0
    tui._pending_invalidate = False
    tui._spinner_visible = False
    tui._spinner_text = FormattedText([])
    tui._busy = False
    tui._inflight_tokens_in = 0
    tui._inflight_tokens_out = 0
    tui._inflight_started_at = None
    tui._thinking_buffer = ""
    tui._thinking_open = False
    tui._picker = None
    tui._approval_pending = None
    tui._mode_idx = 0
    tui._mode_changed_at = 0.0
    tui._last_model = ""
    tui._last_tokens = ""
    tui._last_cost = ""
    tui._update_info = None
    tui._update_dismissed = False
    tui._new_lines_below = 0
    tui._user_scrolled_up = False

    buf = Buffer(read_only=True, name="output_t411")
    buf.set_document(Document("", 0), bypass_readonly=True)
    tui._output_buffer = buf

    app = MagicMock()
    app.output.get_size.return_value.columns = 80
    tui._app = app
    return tui


class TestPickerFloatOverlay:
    """T4.11 — picker Float overlay; _raw_output unchanged when picker is open."""

    def test_dismiss_picker_restores_from_raw_output(self):
        """T4.11 — dismiss restores output buffer from _raw_output, not saved_output."""
        tui = _make_tui()
        tui._raw_output = "canonical output text"

        from axiom.extensions.builtins.chat.fullscreen import PickerMode, PickerState

        # Simulate picker open: buffer has picker content, _raw_output unchanged
        tui._picker = PickerState(mode=PickerMode.SELECT, items=[], cursor=0)
        tui._output_buffer.set_document(
            Document("picker overlay content", 0), bypass_readonly=True
        )

        tui._dismiss_picker()

        assert tui._picker is None
        assert tui._output_buffer.text == "canonical output text", (
            "_dismiss_picker must restore from _raw_output, not from stale picker buffer"
        )

    def test_render_picker_does_not_write_to_output_buffer(self):
        """T4.11 — _render_picker must not mutate _output_buffer."""
        tui = _make_tui()
        original_text = "existing output stays"
        tui._raw_output = original_text
        tui._output_buffer.set_document(Document(original_text, 0), bypass_readonly=True)

        from axiom.extensions.builtins.chat.fullscreen import PickerMode, PickerState

        tui._picker = PickerState(
            mode=PickerMode.SELECT,
            items=[{"id": "abc123", "title": "Test session", "message_count": 2, "updated_at": ""}],
            cursor=0,
        )
        tui._render_picker()

        assert tui._output_buffer.text == original_text, (
            "_render_picker must not write to _output_buffer when using Float overlay"
        )

    def test_picker_float_content_includes_items(self):
        """T4.11 — _picker_float_content returns text listing picker items."""
        tui = _make_tui()

        from axiom.extensions.builtins.chat.fullscreen import PickerMode, PickerState

        tui._picker = PickerState(
            mode=PickerMode.SELECT,
            items=[
                {"id": "abc123456789", "title": "My session", "message_count": 5, "updated_at": ""},
            ],
            cursor=0,
        )
        result = tui._picker_float_content()
        # Result should be FormattedText (list of tuples)
        text_parts = [text for _style, text in result]
        combined = "".join(text_parts)
        assert "abc123456789"[:12] in combined
        assert "My session" in combined
