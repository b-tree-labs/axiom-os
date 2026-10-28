# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""T2.2 — tail-only rewrap + align-table cache performance tests."""

from __future__ import annotations

import threading
from unittest.mock import MagicMock, patch

from prompt_toolkit.buffer import Buffer
from prompt_toolkit.document import Document


def _make_minimal_tui():
    """Return a bare FullScreenChat instance with all app plumbing mocked out."""
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

    buf = Buffer(read_only=True, name="output_test")
    buf.set_document(Document("", 0), bypass_readonly=True)
    tui._output_buffer = buf

    app = MagicMock()
    app.output.get_size.return_value.columns = 80
    tui._app = app
    return tui


class TestTailOnlyRewrap:
    """_append_output must not re-wrap already-committed lines (O(N) not O(N²))."""

    def test_append_output_does_not_rewrap_committed_lines(self):
        """Each _word_wrap call should process at most 1 line, not all accumulated text."""
        from axiom.extensions.builtins.chat.fullscreen import FullScreenChat

        tui = _make_minimal_tui()

        max_input_len = [0]
        original_word_wrap = FullScreenChat._word_wrap

        def tracking_wrap(self_arg, text, width):
            max_input_len[0] = max(max_input_len[0], len(text))
            return original_word_wrap(self_arg, text, width)

        with patch.object(
            type(tui), "_word_wrap", side_effect=tracking_wrap, autospec=True
        ):
            for i in range(100):
                tui._append_output(f"line {i:03d}\n")

        # Longest single line is "line 099\n" = 9 chars; allow some headroom.
        # With O(N²) re-wrap the 100th call would pass ~900 chars.
        assert max_input_len[0] <= 20, (
            f"O(N²) re-wrap detected: _word_wrap was called with "
            f"{max_input_len[0]} chars on a single append (expected <= 20 "
            f"for one-line tail-only rewrap)"
        )

    def test_committed_lines_accumulate_correctly(self):
        tui = _make_minimal_tui()

        for i in range(5):
            tui._append_output(f"line{i}\n")

        # All 5 lines should be in the output buffer
        doc_text = tui._output_buffer.text
        for i in range(5):
            assert f"line{i}" in doc_text

    def test_partial_tail_flushes_on_newline(self):
        tui = _make_minimal_tui()

        tui._append_output("hello ")
        tui._append_output("world\n")

        assert "hello world" in tui._output_buffer.text


class TestAlignTableCache:
    """_word_wrap must cache table alignment by (content_hash, width)."""

    def test_align_table_caches_repeated_layout(self):
        from axiom.extensions.builtins.chat.fullscreen import _align_table

        align_call_count = [0]
        original_align = _align_table

        def counting_align(lines, max_width):
            align_call_count[0] += 1
            return original_align(lines, max_width)

        # Single table block (3 lines terminated by \n)
        table = "| Col A | Col B |\n|-------|-------|\n| a     | b     |\n"

        tui = _make_minimal_tui()

        with patch(
            "axiom.extensions.builtins.chat.fullscreen._align_table",
            side_effect=counting_align,
        ):
            tui._append_output(table)
            # Reset two-buffer state so the same table triggers a second wrap call
            # but the CACHE should absorb it without re-aligning
            tui._wrapped_committed = ""
            tui._partial_tail = ""
            tui._table_pending = []
            tui._append_output(table)

        assert align_call_count[0] <= 1, (
            f"Table alignment was computed {align_call_count[0]} times "
            f"for identical table + width (expected <= 1 via cache)"
        )
