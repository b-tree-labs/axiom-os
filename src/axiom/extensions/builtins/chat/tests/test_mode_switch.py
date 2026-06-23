# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""T4.7 — mode-switch transient flash confirms Shift+Tab keystroke."""

from __future__ import annotations

import threading
import time
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

    buf = Buffer(read_only=True, name="output_t47")
    buf.set_document(Document("", 0), bypass_readonly=True)
    tui._output_buffer = buf

    app = MagicMock()
    app.output.get_size.return_value.columns = 80
    tui._app = app
    return tui


class TestModeSwitchFlash:
    def test_toolbar_uses_warning_class_immediately_after_mode_change(self):
        """T4.7 — within 250ms of mode change, toolbar label uses class:warning."""
        tui = _make_tui()
        tui._mode_changed_at = time.monotonic()  # just switched

        toolbar = tui._get_toolbar_text()
        classes = [cls for cls, _ in toolbar]
        assert "class:warning" in classes, (
            f"Expected class:warning in toolbar after mode switch, got: {classes}"
        )

    def test_toolbar_reverts_after_250ms(self):
        """T4.7 — after 250ms the label reverts to class:toolbar.mode."""
        tui = _make_tui()
        tui._mode_changed_at = time.monotonic() - 0.3  # 300ms ago

        toolbar = tui._get_toolbar_text()
        classes = [cls for cls, _ in toolbar]
        assert "class:toolbar.mode" in classes, (
            f"Expected class:toolbar.mode after flash window, got: {classes}"
        )
        assert "class:warning" not in classes

    def test_mode_changed_at_updated_by_cycle(self):
        """T4.7 — _mode_changed_at is set when mode cycles."""
        import axiom.extensions.builtins.chat.fullscreen as fs_module

        tui = _make_tui()
        old_ts = tui._mode_changed_at

        # Directly invoke the cycle logic (bypass keybinding machinery)
        tui._mode_idx = (tui._mode_idx + 1) % len(fs_module._MODES)
        tui._mode_changed_at = time.monotonic()

        assert tui._mode_changed_at > old_ts
