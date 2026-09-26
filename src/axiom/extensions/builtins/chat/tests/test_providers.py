# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tests for chat providers — factory auto-detection, provider fallback."""

from unittest.mock import MagicMock, patch

import pytest

from axiom.extensions.builtins.chat.provider_factory import (
    create_input_provider,
    create_render_provider,
)
from axiom.extensions.builtins.chat.providers.ansi_render import AnsiRenderProvider
from axiom.extensions.builtins.chat.providers.base import InputProvider, RenderProvider
from axiom.extensions.builtins.chat.providers.basic_input import BasicInputProvider
from axiom.infra.gateway import StreamChunk
from axiom.setup.renderer import set_color_enabled


@pytest.fixture(autouse=True)
def disable_color():
    set_color_enabled(False)
    yield
    set_color_enabled(False)


class TestRenderProviderABC:
    """Test that AnsiRenderProvider implements the full interface."""

    def test_ansi_is_render_provider(self):
        p = AnsiRenderProvider()
        assert isinstance(p, RenderProvider)

    def test_all_methods_exist(self):
        p = AnsiRenderProvider()
        assert callable(p.stream_text)
        assert callable(p.render_welcome)
        assert callable(p.render_tool_start)
        assert callable(p.render_tool_result)
        assert callable(p.render_approval_prompt)
        assert callable(p.render_action_result)
        assert callable(p.render_status)
        assert callable(p.render_thinking)
        assert callable(p.render_message)
        assert callable(p.render_session_list)


class TestInputProviderABC:
    """Test that BasicInputProvider implements the full interface."""

    def test_basic_is_input_provider(self):
        p = BasicInputProvider()
        assert isinstance(p, InputProvider)

    def test_setup_teardown(self):
        p = BasicInputProvider()
        p.setup(slash_commands=["/help", "/exit"])
        p.teardown()


class TestProviderFactory:
    """Test auto-detection and forced provider creation."""

    def test_force_ansi_render(self):
        p = create_render_provider(force="ansi")
        assert isinstance(p, AnsiRenderProvider)

    def test_force_basic_input(self):
        p = create_input_provider(force="basic")
        assert isinstance(p, BasicInputProvider)

    def test_auto_detect_render_without_rich(self):
        with patch(
            "axiom.extensions.builtins.chat.provider_factory._rich_available",
            return_value=False,
        ):
            p = create_render_provider()
            assert isinstance(p, AnsiRenderProvider)

    def test_auto_detect_input_without_ptk(self):
        with patch(
            "axiom.extensions.builtins.chat.provider_factory._ptk_available",
            return_value=False,
        ):
            p = create_input_provider()
            assert isinstance(p, BasicInputProvider)

    def test_force_rich_falls_back_on_import_error(self):
        # When rich is explicitly requested but import fails
        with patch(
            "axiom.extensions.builtins.chat.provider_factory._rich_available",
            return_value=True,
        ):
            # Mock the import to fail
            with patch(
                "axiom.extensions.builtins.chat.providers.rich_render.RichRenderProvider",
                side_effect=ImportError("no rich"),
            ):
                p = create_render_provider(force="rich")
                assert isinstance(p, AnsiRenderProvider)


class TestAnsiRenderProvider:
    """Test ANSI render provider output."""

    def test_stream_text(self, capsys):
        p = AnsiRenderProvider()
        chunks = iter(
            [
                StreamChunk(type="text", text="Hello "),
                StreamChunk(type="text", text="world!"),
                StreamChunk(type="done"),
            ]
        )
        result = p.stream_text(chunks)
        assert result == "Hello world!"
        captured = capsys.readouterr()
        assert "Hello" in captured.out

    def test_stream_with_tool_use(self, capsys):
        p = AnsiRenderProvider()
        chunks = iter(
            [
                StreamChunk(type="text", text="Checking.\n"),
                StreamChunk(type="tool_use_start", tool_name="query_docs", tool_id="t1"),
                StreamChunk(type="tool_use_end", tool_name="query_docs", tool_id="t1"),
                StreamChunk(type="done"),
            ]
        )
        result = p.stream_text(chunks)
        assert "Checking." in result

    def test_render_welcome(self, capsys):
        p = AnsiRenderProvider()
        p.render_welcome()
        captured = capsys.readouterr()
        assert "axi chat" in captured.out
        assert "/help" in captured.out

    def test_render_welcome_with_gateway(self, capsys):
        p = AnsiRenderProvider()
        gw = MagicMock()
        gw.active_provider = None
        p.render_welcome(gateway=gw)
        captured = capsys.readouterr()
        assert "No LLM configured" in captured.out

    def test_render_welcome_no_gateway_uses_dim_not_yellow(self, capsys):
        """T3.11 — no-LLM empty state is dim, not yellow."""
        from axiom.setup.renderer import set_color_enabled

        set_color_enabled(True)
        p = AnsiRenderProvider()
        gw = MagicMock()
        gw.active_provider = None
        p.render_welcome(gateway=gw)
        captured = capsys.readouterr()
        assert "\x1b[33m" not in captured.out  # no yellow
        set_color_enabled(False)

    def test_render_tool_result_success(self, capsys):
        p = AnsiRenderProvider()
        p.render_tool_result("query_docs", {"documents": []}, 0.5)
        captured = capsys.readouterr()
        assert "query_docs" in captured.out
        assert "0.5s" in captured.out

    def test_render_tool_result_error(self, capsys):
        p = AnsiRenderProvider()
        p.render_tool_result("doc_publish", {"error": "not found"}, 1.2)
        captured = capsys.readouterr()
        assert "failed" in captured.out
        assert "not found" in captured.out

    def test_render_status(self, capsys):
        p = AnsiRenderProvider()
        p.render_status("claude-3-sonnet", 1000, 500, 0.0075)
        captured = capsys.readouterr()
        assert "1,000" in captured.out  # thousands separator
        assert "500" in captured.out
        assert "$0.0075" in captured.out

    def test_render_status_uses_middle_dots_and_thousands(self, capsys):
        """T3.5 — middle-dot separators and comma thousands formatting."""
        p = AnsiRenderProvider()
        p.render_status("claude-3-opus", 12345, 6789, 0.0123)
        captured = capsys.readouterr()
        assert "·" in captured.out  # middle-dot separator
        assert "12,345" in captured.out
        assert "6,789" in captured.out
        assert "|" not in captured.out  # no pipe separators

    def test_render_status_shows_routing_tier_when_set(self, capsys):
        """T3.5 — optional tier kwarg appears in output."""
        p = AnsiRenderProvider()
        p.render_status("claude-3-haiku", 100, 50, 0.001, tier="export_controlled")
        captured = capsys.readouterr()
        assert "export_controlled" in captured.out

    def test_render_thinking_collapsed(self, capsys):
        p = AnsiRenderProvider()
        text = "\n".join(f"Line {i}" for i in range(10))
        p.render_thinking(text, collapsed=True)
        captured = capsys.readouterr()
        assert "thinking" in captured.out.lower()
        assert "more lines" in captured.out

    def test_thinking_truncation_includes_expand_hint(self, capsys):
        """T3.6 — truncation message uses pluralize and Alt+T hint."""
        p = AnsiRenderProvider()
        text = "\n".join(f"Line {i}" for i in range(10))
        p.render_thinking(text, collapsed=True)
        captured = capsys.readouterr()
        assert "Alt+T to expand" in captured.out
        assert "7 more lines" in captured.out  # 10 - 3 = 7

    def test_thinking_prefix_spacing(self, capsys):
        """T4.9 — header is '  [thinking]', body lines use '  │ ' gutter (2-space indent)."""
        p = AnsiRenderProvider()
        p.render_thinking("hello", collapsed=False)
        captured = capsys.readouterr()
        lines = [ln for ln in captured.out.splitlines() if ln.strip()]
        assert lines[0].startswith("  [thinking]"), f"first line: {lines[0]!r}"
        body_lines = lines[1:]
        for ln in body_lines:
            assert ln.startswith("  "), f"bad indent: {ln!r}"

    def test_render_thinking_empty(self, capsys):
        p = AnsiRenderProvider()
        p.render_thinking("")
        captured = capsys.readouterr()
        assert captured.out == ""

    def test_thinking_header_appears_once_not_many(self, capsys):
        """T4.9 — [thinking] header printed once, body lines get │ prefix."""
        p = AnsiRenderProvider()
        text = "Line one\nLine two\nLine three"
        p.render_thinking(text, collapsed=False)
        captured = capsys.readouterr()
        lines = captured.out.splitlines()
        thinking_header_count = sum(1 for ln in lines if "[thinking]" in ln and "│" not in ln)
        thinking_body_count = sum(1 for ln in lines if "│" in ln)
        # Exactly one header line, body lines have the left-rail prefix
        assert thinking_header_count == 1, (
            f"expected 1 [thinking] header, got {thinking_header_count}: {lines}"
        )
        assert thinking_body_count >= 1, f"expected │ body lines, got none: {lines}"

    def test_render_message_assistant(self, capsys):
        p = AnsiRenderProvider()
        p.render_message("assistant", "Hello there!")
        captured = capsys.readouterr()
        assert "Hello there!" in captured.out

    def test_render_message_user_noop(self, capsys):
        p = AnsiRenderProvider()
        p.render_message("user", "test input")
        captured = capsys.readouterr()
        assert captured.out == ""

    def test_render_session_list_empty(self, capsys):
        p = AnsiRenderProvider()
        p.render_session_list([])
        captured = capsys.readouterr()
        assert "No saved sessions" in captured.out

    def test_empty_states_use_dim_not_yellow(self, capsys):
        """T3.11 — empty state uses DIM (\\x1b[2m), not YELLOW (\\x1b[33m)."""
        from axiom.setup.renderer import set_color_enabled

        set_color_enabled(True)
        p = AnsiRenderProvider()
        p.render_session_list([])
        captured = capsys.readouterr()
        assert "\x1b[33m" not in captured.out  # no yellow
        assert "\x1b[2m" in captured.out  # DIM present
        set_color_enabled(False)

    def test_render_session_list(self, capsys):
        p = AnsiRenderProvider()
        sessions = [
            {"id": "abc123", "messages": 5, "updated": "2026-02-19"},
        ]
        p.render_session_list(sessions)
        captured = capsys.readouterr()
        assert "abc123" in captured.out
        assert "5 messages" in captured.out

    def test_session_list_pluralizes_one_message(self, capsys):
        """T3.7 — 1 message (not 1 messages)."""
        p = AnsiRenderProvider()
        sessions = [{"id": "s1", "messages": 1, "updated": ""}]
        p.render_session_list(sessions)
        captured = capsys.readouterr()
        assert "1 message" in captured.out
        assert "1 messages" not in captured.out

    def test_session_list_columns_align(self, capsys):
        """T3.7 — fixed-width columns, all lines start with 2 spaces."""
        p = AnsiRenderProvider()
        sessions = [
            {"id": "shortid", "messages": 3, "updated": "2026-01-01T00:00:00Z"},
            {"id": "longer-session-id", "messages": 42, "updated": "2026-01-02T00:00:00Z"},
        ]
        p.render_session_list(sessions)
        captured = capsys.readouterr()
        for line in captured.out.splitlines():
            if line.strip() and "shortid" in line or "longer-session" in line:
                assert line.startswith("  ")

    def test_session_list_includes_navigation_hint(self, capsys):
        """T3.7 — navigation hint line at end."""
        p = AnsiRenderProvider()
        sessions = [{"id": "s1", "messages": 2, "updated": ""}]
        p.render_session_list(sessions)
        captured = capsys.readouterr()
        assert "/resume" in captured.out

    def test_session_list_includes_relative_time(self, capsys):
        """T3.7 — time_ago is used when updated is a valid timestamp."""
        from datetime import UTC, datetime, timedelta

        p = AnsiRenderProvider()
        ts = (datetime.now(UTC) - timedelta(days=2)).isoformat()
        sessions = [{"id": "s1", "messages": 1, "updated": ts}]
        p.render_session_list(sessions)
        captured = capsys.readouterr()
        assert "ago" in captured.out  # time_ago output

    def test_render_action_result_completed(self, capsys):
        from axiom.infra.orchestrator.actions import Action

        p = AnsiRenderProvider()
        action = Action(name="query_docs", params={})
        action.complete({"documents": []})
        p.render_action_result(action)
        captured = capsys.readouterr()
        assert "No tracked documents" in captured.out

    def test_render_action_result_rejected(self, capsys):
        from axiom.infra.orchestrator.actions import Action

        p = AnsiRenderProvider()
        action = Action(name="doc_publish", params={})
        action.reject("Not ready")
        p.render_action_result(action)
        captured = capsys.readouterr()
        assert "skipped" in captured.out

    def test_tool_result_no_carriage_return_no_padding(self, capsys):
        """T3.2 — no \\r, no run of 30+ spaces in tool result output."""
        p = AnsiRenderProvider()
        p.render_tool_result("write_file", {}, 0.8)
        captured = capsys.readouterr()
        assert "\r" not in captured.out
        assert "  " * 15 not in captured.out  # no 30+ space padding

    def test_tool_result_uses_unicode_glyphs_when_color_enabled(self, capsys):
        """T3.2 — ✓ on success, ✗ on error when color is on."""
        from axiom.setup.renderer import set_color_enabled

        set_color_enabled(True)
        p = AnsiRenderProvider()
        p.render_tool_result("read_file", {}, 0.3)
        out_ok = capsys.readouterr().out
        p.render_tool_result("write_file", {"error": "denied"}, 0.3)
        out_err = capsys.readouterr().out
        assert "✓" in out_ok
        assert "✗" in out_err
        set_color_enabled(False)

    def test_tool_result_ascii_fallback_when_color_disabled(self, capsys):
        """T3.2 — OK/FAIL fallback when color is disabled."""
        p = AnsiRenderProvider()  # color already disabled by fixture
        p.render_tool_result("read_file", {}, 0.3)
        out_ok = capsys.readouterr().out
        p.render_tool_result("write_file", {"error": "denied"}, 0.3)
        out_err = capsys.readouterr().out
        assert "OK" in out_ok
        assert "FAIL" in out_err


# ---------------------------------------------------------------------------
# Tests for ChatCompleter (slash palette, @file, /model, /resume arg-completion)
# ---------------------------------------------------------------------------

_PTK_SKIP = pytest.mark.skipif(
    not pytest.importorskip("prompt_toolkit", reason="prompt_toolkit not installed")
    if False
    else False,
    reason="prompt_toolkit not available",
)


@pytest.fixture
def _ptk():
    pytest.importorskip("prompt_toolkit")


class TestChatCompleter:
    """Tests for the ChatCompleter added in T1.6."""

    def _make_completer(self, slash_cmds=None, providers=None, sessions=None):
        from axiom.extensions.builtins.chat.providers.ptk_input import ChatCompleter

        slash_cmds = slash_cmds or {
            "/help": "Show help",
            "/model": "Switch model",
            "/resume": "Resume session",
            "/exit": "Exit",
        }
        providers_fn = (lambda: providers) if providers is not None else None
        sessions_fn = (lambda: sessions) if sessions is not None else None
        return ChatCompleter(
            slash_commands=slash_cmds,
            providers_fn=providers_fn,
            sessions_fn=sessions_fn,
        )

    def _completions(self, completer, text):
        from prompt_toolkit.document import Document

        doc = Document(text, cursor_position=len(text))
        return list(completer.get_completions(doc, None))

    def _display_texts(self, completions):
        """Return the display text of each completion."""
        return [str(cp.display) for cp in completions]

    def test_slash_palette_filters_by_prefix(self, _ptk):
        """Typing '/h' should complete to /help but not /model."""
        c = self._make_completer()
        completions = self._completions(c, "/h")
        displays = self._display_texts(completions)
        assert any("help" in d for d in displays)
        assert not any("model" in d for d in displays)

    def test_slash_palette_includes_descriptions(self, _ptk):
        """Completions for slash commands include description as display_meta."""
        c = self._make_completer()
        completions = self._completions(c, "/he")
        assert completions, "expected at least one completion for /he"
        meta = str(completions[0].display_meta)
        assert "help" in meta.lower() or "Show" in meta

    def test_model_arg_completion(self, _ptk):
        """/model <tab> completes with provider names."""
        c = self._make_completer(providers=["anthropic", "ollama"])
        completions = self._completions(c, "/model ")
        texts = [cp.text for cp in completions]
        assert "anthropic" in texts
        assert "ollama" in texts

    def test_resume_arg_completion(self, _ptk):
        """/resume <tab> completes with session IDs."""
        c = self._make_completer(sessions=["abc-123", "def-456"])
        completions = self._completions(c, "/resume ")
        texts = [cp.text for cp in completions]
        assert "abc-123" in texts

    def test_at_file_completion(self, _ptk, tmp_path):
        """@path triggers PathCompleter for file paths."""
        from axiom.extensions.builtins.chat.providers.ptk_input import ChatCompleter

        # Create a real file so PathCompleter can find it
        (tmp_path / "README.md").write_text("test")
        c = ChatCompleter(slash_commands={})
        completions = self._completions(c, f"@{tmp_path}/")
        all_text = [cp.text for cp in completions] + [str(cp.display) for cp in completions]
        assert any("README.md" in t for t in all_text)

    def test_multi_word_commands_skipped_in_palette(self, _ptk):
        """/sessions rename should not appear in palette (multi-word)."""
        slash_cmds = {
            "/help": "Help",
            "/sessions": "Sessions",
            "/sessions rename": "Rename session",
        }
        c = self._make_completer(slash_cmds=slash_cmds)
        completions = self._completions(c, "/")
        displays = self._display_texts(completions)
        # /sessions rename is multi-word and should not be in the palette
        assert "/sessions rename" not in displays


class TestToolbarHonesty:
    """T1.8 — toolbar must not promise 'esc to interrupt' (not yet implemented)."""

    def test_toolbar_does_not_promise_esc_interrupt(self, _ptk):
        from axiom.extensions.builtins.chat.providers.ptk_input import PTKInputProvider

        p = PTKInputProvider()
        # _build_toolbar returns a list of (style, text) tuples
        toolbar = p._build_toolbar()
        all_text = " ".join(text for _, text in toolbar)
        assert "esc to interrupt" not in all_text.lower()


class TestSlashCommandQueue:
    """Tests for enqueue/drain_queue on PTKInputProvider."""

    def test_enqueue_and_drain(self, _ptk):
        from axiom.extensions.builtins.chat.providers.ptk_input import PTKInputProvider

        p = PTKInputProvider()
        p.enqueue("/status")
        p.enqueue("/usage")
        drained = p.drain_queue()
        assert drained == ["/status", "/usage"]

    def test_drain_clears_queue(self, _ptk):
        from axiom.extensions.builtins.chat.providers.ptk_input import PTKInputProvider

        p = PTKInputProvider()
        p.enqueue("/status")
        p.drain_queue()
        assert p.drain_queue() == []


# ---------------------------------------------------------------------------
# T4.1 — Cherenkov gutter rail appears in render_welcome
# ---------------------------------------------------------------------------


class TestGutterRailInWelcome:
    def test_render_welcome_includes_gutter_rail(self, capsys):
        """T4.1 — render_welcome header line has the Cherenkov gutter char."""
        p = AnsiRenderProvider()
        p.render_welcome()
        captured = capsys.readouterr()
        assert "▎" in captured.out, "Cherenkov gutter char missing from welcome output"
