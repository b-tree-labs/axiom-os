# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""T2.3 — tool input deltas streamed to output buffer.
T2.5 — live status line tokens.
T2.6 — thinking buffer accumulation.
"""

from __future__ import annotations

import threading
from unittest.mock import MagicMock

from prompt_toolkit.buffer import Buffer
from prompt_toolkit.document import Document


def _make_tui_with_provider():
    """Build a minimal FullScreenChat + _TuiRenderProvider pair."""
    from axiom.extensions.builtins.chat.fullscreen import FullScreenChat, _TuiRenderProvider

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
    tui._busy = False
    # T2.5 inflight tracking
    tui._inflight_tokens_in = 0
    tui._inflight_tokens_out = 0
    tui._inflight_started_at = None
    # T2.6 thinking
    tui._thinking_buffer = ""
    tui._thinking_open = False

    buf = Buffer(read_only=True, name="output_test2")
    buf.set_document(Document("", 0), bypass_readonly=True)
    tui._output_buffer = buf

    app = MagicMock()
    app.output.get_size.return_value.columns = 80
    tui._app = app

    tui._stop_spinner = MagicMock()
    tui._mermaid_dir = None
    tui._mermaid_cache = {}
    # A4 math-fence integration: _process_math walks this cache
    tui._math_cache = {}

    provider = _TuiRenderProvider(tui)
    return tui, provider


def _make_chunk(type_, **kwargs):
    from axiom.infra.gateway import StreamChunk

    return StreamChunk(type=type_, **kwargs)


class TestToolChunksRoutedToOutput:
    """T2.3 — tool_use_start/tool_input_delta/tool_use_end appear in output buffer."""

    def test_tool_use_start_appends_to_output_buffer(self):
        tui, provider = _make_tui_with_provider()

        chunks = [
            _make_chunk("text", text="Let me write that file."),
            _make_chunk("tool_use_start", tool_name="write_file", tool_id="t1"),
            _make_chunk(
                "tool_input_delta",
                tool_id="t1",
                tool_input_json='{"file_path": "foo.txt"}',
            ),
            _make_chunk("tool_use_end", tool_id="t1", tool_input_json='{"file_path": "foo.txt"}'),
            _make_chunk("done"),
        ]

        provider.stream_text(iter(chunks))

        raw = tui._raw_output
        assert "[tool] write_file" in raw, f"Expected tool header in output, got: {raw!r}"
        assert "foo.txt" in raw, f"Expected tool input in output, got: {raw!r}"

    def test_tool_use_end_adds_closer(self):
        tui, provider = _make_tui_with_provider()

        chunks = [
            _make_chunk("tool_use_start", tool_name="read_file", tool_id="t2"),
            _make_chunk("tool_input_delta", tool_id="t2", tool_input_json='{"path": "x"}'),
            _make_chunk("tool_use_end", tool_id="t2", tool_input_json='{"path": "x"}'),
            _make_chunk("done"),
        ]

        provider.stream_text(iter(chunks))

        raw = tui._raw_output
        assert "└" in raw, f"Expected └ closer in output, got: {raw!r}"


class TestInflightTokensTracking:
    """T2.5 — usage chunks update inflight token counters."""

    def test_usage_chunks_update_inflight_counters(self):
        tui, provider = _make_tui_with_provider()

        chunks = [
            _make_chunk("text", text="Hello"),
            _make_chunk("usage", input_tokens=42, output_tokens=17, cache_read_tokens=0),
            _make_chunk("done"),
        ]

        provider.stream_text(iter(chunks))

        assert tui._inflight_tokens_in == 42
        assert tui._inflight_tokens_out == 17


class TestLiveStatusLine:
    """T2.5 — _get_status_text reflects inflight tokens and context % when busy."""

    def test_status_text_reflects_inflight_tokens_during_streaming(self):
        from prompt_toolkit.buffer import Buffer
        from prompt_toolkit.document import Document

        from axiom.extensions.builtins.chat.fullscreen import FullScreenChat

        tui = object.__new__(FullScreenChat)
        tui._output_buffer = Buffer(read_only=True, name="sb")
        tui._output_buffer.set_document(Document("", 0), bypass_readonly=True)
        tui._busy = True
        tui._inflight_tokens_in = 100
        tui._inflight_tokens_out = 50
        tui._inflight_started_at = 1.0
        tui._last_model = "sonnet"
        tui._last_tokens = ""
        tui._last_cost = ""
        tui._update_info = None
        tui._update_dismissed = False

        app = MagicMock()
        app.output.get_size.return_value.columns = 100
        tui._app = app

        # Simulate a session with some messages for ctx %
        session = MagicMock()
        session.messages = []
        agent = MagicMock()
        agent.session = session
        tui._agent = agent

        fragments = tui._get_status_text()
        text = "".join(t for _, t in fragments)

        assert "100in" in text or "streaming" in text.lower() or "50out" in text, (
            f"Expected inflight token counts in status, got: {text!r}"
        )

    def test_status_text_shows_context_pct(self):
        from prompt_toolkit.buffer import Buffer
        from prompt_toolkit.document import Document

        from axiom.extensions.builtins.chat.fullscreen import FullScreenChat

        tui = object.__new__(FullScreenChat)
        tui._output_buffer = Buffer(read_only=True, name="sb2")
        tui._output_buffer.set_document(Document("", 0), bypass_readonly=True)
        tui._busy = False
        tui._inflight_tokens_in = 0
        tui._inflight_tokens_out = 0
        tui._inflight_started_at = None
        tui._last_model = "sonnet"
        tui._last_tokens = "10in/5out"
        tui._last_cost = ""
        tui._update_info = None
        tui._update_dismissed = False

        app = MagicMock()
        app.output.get_size.return_value.columns = 100
        tui._app = app

        # Simulate 5000 chars in session messages (~1.2% of 128k budget)
        msg = MagicMock()
        msg.content = "x" * 5000
        session = MagicMock()
        session.messages = [msg]
        agent = MagicMock()
        agent.session = session
        tui._agent = agent

        fragments = tui._get_status_text()
        text = "".join(t for _, t in fragments)

        assert "ctx" in text, f"Expected 'ctx' in status text, got: {text!r}"


class TestThinkingBufferAccumulation:
    """T2.6 — thinking_delta chunks go to _thinking_buffer, not main output."""

    def test_thinking_buffer_accumulates_separately_from_output(self):
        tui, provider = _make_tui_with_provider()

        chunks = [
            _make_chunk("text", text="Here is my answer."),
            _make_chunk("thinking_delta", text="step 1: analyse"),
            _make_chunk("thinking_delta", text=", step 2: respond"),
            _make_chunk("done"),
        ]

        provider.stream_text(iter(chunks))

        assert "Here is my answer." in tui._raw_output
        assert "step 1: analyse" not in tui._raw_output, (
            "Thinking content must not leak into the main output"
        )
        assert "step 1: analyse" in tui._thinking_buffer
        assert "step 2: respond" in tui._thinking_buffer


def _make_tui_for_t312():
    """Minimal FullScreenChat for T3.12 tests (adds _user_scrolled_up state)."""
    from axiom.extensions.builtins.chat.fullscreen import FullScreenChat

    tui = object.__new__(FullScreenChat)
    tui._output_lock = __import__("threading").Lock()
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
    tui._busy = False
    tui._inflight_tokens_in = 0
    tui._inflight_tokens_out = 0
    tui._inflight_started_at = None
    tui._thinking_buffer = ""
    tui._thinking_open = False
    tui._user_scrolled_up = False
    tui._new_lines_below = 0

    buf = Buffer(read_only=True, name="output_t312")
    buf.set_document(Document("", 0), bypass_readonly=True)
    tui._output_buffer = buf

    app = MagicMock()
    app.output.get_size.return_value.columns = 80
    tui._app = app
    tui._stop_spinner = MagicMock()
    tui._mermaid_dir = None
    tui._mermaid_cache = {}
    # A4 math-fence integration: _process_math walks this cache
    tui._math_cache = {}
    return tui


class TestInputContinuationCue:
    """T3.12A — multi-line input continuation prefix."""

    def test_input_continuation_shows_visual_cue(self):
        """Second and later lines in multi-line input get '··· ' prefix."""
        from axiom.extensions.builtins.chat.fullscreen import FullScreenChat

        tui = object.__new__(FullScreenChat)
        # Minimal state for _get_input_line_prefix
        tui._picker = None
        tui._approval_pending = None

        result = tui._get_input_line_prefix(lineno=1, wrap_count=0)
        text = "".join(t for _, t in result)
        assert "···" in text, f"Expected continuation cue '···', got: {text!r}"

    def test_first_line_has_no_continuation_prefix(self):
        """Line 0 (first) should not get the continuation prefix."""
        from axiom.extensions.builtins.chat.fullscreen import FullScreenChat

        tui = object.__new__(FullScreenChat)
        tui._picker = None
        tui._approval_pending = None

        result = tui._get_input_line_prefix(lineno=0, wrap_count=0)
        text = "".join(t for _, t in result)
        assert "···" not in text


class TestNewContentIndicator:
    """T3.12B — scrolled-up indicator in status bar."""

    def test_new_content_indicator_when_scrolled_up(self):
        """When _user_scrolled_up and _new_lines_below > 0, status shows indicator."""
        from axiom.extensions.builtins.chat.fullscreen import FullScreenChat

        tui = object.__new__(FullScreenChat)
        buf = Buffer(read_only=True, name="output_ncb")
        # Place cursor not at end to simulate scrolled-up state
        text = "line1\nline2\nline3\n"
        buf.set_document(Document(text, 0), bypass_readonly=True)
        tui._output_buffer = buf
        tui._busy = False
        tui._inflight_tokens_in = 0
        tui._inflight_tokens_out = 0
        tui._inflight_started_at = None
        tui._last_model = "sonnet"
        tui._last_tokens = ""
        tui._last_cost = ""
        tui._update_info = None
        tui._update_dismissed = False
        tui._user_scrolled_up = True
        tui._new_lines_below = 5

        app = MagicMock()
        app.output.get_size.return_value.columns = 100
        tui._app = app

        session = MagicMock()
        session.messages = []
        agent = MagicMock()
        agent.session = session
        tui._agent = agent

        fragments = tui._get_status_text()
        text_out = "".join(t for _, t in fragments)
        assert "↓" in text_out or "below" in text_out or "new" in text_out.lower(), (
            f"Expected new-content indicator, got: {text_out!r}"
        )
