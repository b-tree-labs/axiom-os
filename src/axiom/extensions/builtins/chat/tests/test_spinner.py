# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""T4.5 — spinner phase-lock, ghost-frame kill, Cherenkov palette."""

from __future__ import annotations

import threading
from unittest.mock import MagicMock


class TestSpinnerPhaseLock:
    """T4.5a — glyph '•' peaks together with brightness peak."""

    def test_bright_glyph_only_at_frames_4_and_5(self):
        from axiom.extensions.builtins.chat.pulse_spinner import PULSE_FRAMES

        for i, frame in enumerate(PULSE_FRAMES):
            if frame.symbol == "•":
                assert i in (4, 5), (
                    f"'•' glyph at frame {i} is outside phase-lock window [4,5]: "
                    f"{PULSE_FRAMES}"
                )

    def test_peak_brightness_at_frames_4_and_5(self):
        from axiom.extensions.builtins.chat.pulse_spinner import PULSE_FRAMES

        peak_g = max(f.g for f in PULSE_FRAMES)
        peak_frames = [i for i, f in enumerate(PULSE_FRAMES) if f.g == peak_g]
        assert 4 in peak_frames or 5 in peak_frames, (
            f"brightness peak frames {peak_frames} don't include 4 or 5"
        )

    def test_cherenkov_peak_matches_brand_color(self):
        from axiom.extensions.builtins.chat.pulse_spinner import PULSE_FRAMES

        peak = max(PULSE_FRAMES, key=lambda f: f.g)
        assert peak.g == 207, f"Cherenkov peak green should be 207, got {peak.g}"
        assert peak.b == 255, f"Cherenkov peak blue should be 255, got {peak.b}"

    def test_trough_not_dead_pixel_navy(self):
        from axiom.extensions.builtins.chat.pulse_spinner import PULSE_FRAMES

        trough = min(PULSE_FRAMES, key=lambda f: f.g)
        assert trough.g >= 80, (
            f"Cherenkov trough green {trough.g} < 80; will read as dead-pixel navy"
        )


class TestFirstTokenSpinnerNow:
    """T4.6 — spinner visible in main thread before worker thread starts."""

    def test_spinner_visible_immediately_after_on_accept(self):
        """Spinner must be True before the worker thread runs any code."""
        import threading

        from prompt_toolkit.buffer import Buffer
        from prompt_toolkit.document import Document

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
        tui._spinner_label = "Thinking"
        tui._spinner_input_tokens = 0
        tui._spinner_output_tokens = 0
        tui._spinner_sub_state = ""
        tui._spinner_start = 0.0
        tui._spinner_stop_event = threading.Event()
        tui._spinner_thread = None
        tui._spinner_text = None
        tui._busy = False
        tui._interrupted = False
        tui._inflight_tokens_in = 0
        tui._inflight_tokens_out = 0
        tui._inflight_started_at = None
        tui._thinking_buffer = ""
        tui._thinking_open = False
        tui._input_history = []
        tui._history_idx = 0
        tui._history_stash = ""
        tui._picker = None
        tui._approval_pending = None
        tui._pending_command = None
        tui._mermaid_dir = None
        tui._mermaid_cache = {}
        tui._mode_idx = 0
        tui._mode_changed_at = 0.0
        tui._stream = False
        tui._store = MagicMock()
        tui._agent = MagicMock()
        tui._agent.gateway.available = False

        buf = Buffer(read_only=True, name="output_t46")
        buf.set_document(Document("", 0), bypass_readonly=True)
        tui._output_buffer = buf

        from prompt_toolkit.formatted_text import FormattedText
        tui._spinner_text = FormattedText([])

        app = MagicMock()
        app.output.get_size.return_value.columns = 80
        tui._app = app

        # Patch _run_agent_turn to block until we release it
        worker_started = threading.Event()
        release_worker = threading.Event()

        def _fake_run(text):
            worker_started.set()
            release_worker.wait(timeout=2.0)
            tui._busy = False

        tui._run_agent_turn = _fake_run
        tui._stop_spinner = MagicMock()

        # Simulate _on_accept by calling the actual method
        spinner_visible_before_thread = []


        def patched_start_spinner(self, label="Thinking"):
            spinner_visible_before_thread.append(True)
            self._spinner_visible = True

        import axiom.extensions.builtins.chat.fullscreen as fs_module
        original = fs_module.FullScreenChat._start_spinner
        fs_module.FullScreenChat._start_spinner = patched_start_spinner

        try:
            buff_mock = MagicMock()
            buff_mock.text = "hello world"
            tui._on_accept(buff_mock)

            # Spinner must be visible NOW, in main thread, before worker runs
            assert tui._spinner_visible is True, (
                "spinner not visible in main thread before worker started"
            )
        finally:
            fs_module.FullScreenChat._start_spinner = original
            release_worker.set()


class TestStopSpinnerGhostFrame:
    """T4.5b — _stop_spinner clears text before joining thread."""

    def test_stop_spinner_sets_visible_false_before_join(self):
        from axiom.extensions.builtins.chat.fullscreen import FullScreenChat

        tui = object.__new__(FullScreenChat)
        tui._spinner_stop_event = threading.Event()
        tui._spinner_text_cleared_before_join = False

        # A mock thread whose join() checks the state
        original_text = object()
        tui._spinner_text = original_text
        tui._spinner_visible = True

        join_called_with_visible = []

        class _FakeThread:
            def join(self_, timeout=None):
                join_called_with_visible.append(tui._spinner_visible)

        tui._spinner_thread = _FakeThread()
        app = MagicMock()
        tui._app = app

        tui._stop_spinner()

        # _spinner_visible must be False by the time join() is called
        assert join_called_with_visible == [False], (
            f"_spinner_visible was {join_called_with_visible} during join"
        )
        assert tui._spinner_visible is False

    def test_stop_spinner_clears_spinner_text_to_empty(self):
        from prompt_toolkit.formatted_text import FormattedText

        from axiom.extensions.builtins.chat.fullscreen import FullScreenChat

        tui = object.__new__(FullScreenChat)
        tui._spinner_stop_event = threading.Event()
        tui._spinner_text = FormattedText([("class:test", "Thinking…")])
        tui._spinner_visible = True
        tui._spinner_thread = None
        tui._app = MagicMock()

        tui._stop_spinner()

        assert tui._spinner_text == FormattedText([])
