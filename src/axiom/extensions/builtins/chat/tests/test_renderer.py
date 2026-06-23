# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tests for the chat renderer — markdown formatting, streaming, approval UI."""

import pytest

from axiom.extensions.builtins.chat.renderer import (
    _format_params,
    format_markdown_line,
    render_message,
    render_session_list,
    render_welcome,
    stream_text,
)
from axiom.infra.gateway import StreamChunk
from axiom.setup.renderer import set_color_enabled


@pytest.fixture(autouse=True)
def disable_color():
    """Disable color for predictable test output."""
    set_color_enabled(False)
    yield
    set_color_enabled(False)


class TestFormatMarkdownLine:
    """Test basic markdown → ANSI formatting."""

    def test_heading(self):
        result = format_markdown_line("# My Heading")
        # Without color, should return unchanged
        assert "My Heading" in result

    def test_bold(self):
        result = format_markdown_line("This is **bold** text")
        assert "bold" in result

    def test_inline_code(self):
        result = format_markdown_line("Use `query_docs` to check")
        assert "query_docs" in result

    def test_list_item(self):
        result = format_markdown_line("- First item")
        assert "First item" in result

    def test_code_fence(self):
        result = format_markdown_line("```python")
        assert "python" in result

    def test_plain_text(self):
        result = format_markdown_line("Just some plain text.")
        assert result == "Just some plain text."

    def test_heading_with_color(self):
        set_color_enabled(True)
        result = format_markdown_line("## Section Title")
        assert "\033[" in result  # Contains ANSI codes
        assert "Section Title" in result
        set_color_enabled(False)

    def test_bold_with_color(self):
        set_color_enabled(True)
        result = format_markdown_line("This is **bold** text")
        assert "\033[1m" in result  # BOLD code
        set_color_enabled(False)

    def test_inline_code_with_color(self):
        set_color_enabled(True)
        result = format_markdown_line("Run `neut chat`")
        assert "\033[36m" in result  # CYAN code
        set_color_enabled(False)


class TestStreamText:
    """Test streaming text display."""

    def test_basic_streaming(self, capsys):
        chunks = iter(
            [
                StreamChunk(type="text", text="Hello "),
                StreamChunk(type="text", text="world!"),
                StreamChunk(type="done"),
            ]
        )
        result = stream_text(chunks)
        assert result == "Hello world!"
        captured = capsys.readouterr()
        assert "Hello " in captured.out
        assert "world!" in captured.out

    def test_tool_use_indicator(self, capsys):
        chunks = iter(
            [
                StreamChunk(type="text", text="Let me check. "),
                StreamChunk(type="tool_use_start", tool_name="query_docs", tool_id="t1"),
                StreamChunk(type="tool_use_end", tool_name="query_docs", tool_id="t1"),
                StreamChunk(type="done"),
            ]
        )
        result = stream_text(chunks)
        assert result == "Let me check. "
        captured = capsys.readouterr()
        assert "calling query_docs" in captured.out

    def test_empty_stream(self, capsys):
        chunks = iter([StreamChunk(type="done")])
        result = stream_text(chunks)
        assert result == ""

    def test_multiline_streaming(self, capsys):
        chunks = iter(
            [
                StreamChunk(type="text", text="Line 1\nLine 2\n"),
                StreamChunk(type="done"),
            ]
        )
        result = stream_text(chunks)
        assert "Line 1" in result
        assert "Line 2" in result

    def test_stream_text_indents_two_spaces_like_render_message(self, capsys):
        """T3.1 — every non-empty output line starts with 2-space gutter."""
        chunks = iter(
            [
                StreamChunk(type="text", text="First line\nSecond line\nThird line\n"),
                StreamChunk(type="done"),
            ]
        )
        stream_text(chunks)
        captured = capsys.readouterr()
        non_empty = [ln for ln in captured.out.splitlines() if ln.strip()]
        assert non_empty, "expected some output lines"
        for line in non_empty:
            assert line.startswith("  "), (
                f"line does not start with 2-space indent: {line!r}"
            )


class TestRenderMessage:
    """Test message rendering."""

    def test_assistant_message(self, capsys):
        render_message("assistant", "Hello there!")
        captured = capsys.readouterr()
        assert "Hello there!" in captured.out

    def test_system_message(self, capsys):
        render_message("system", "Connected.")
        captured = capsys.readouterr()
        assert "[system]" in captured.out
        assert "Connected." in captured.out

    def test_user_message_noop(self, capsys):
        render_message("user", "test")
        captured = capsys.readouterr()
        assert captured.out == ""


class TestRenderWelcome:
    """Test welcome message rendering."""

    def test_basic_welcome(self, capsys):
        render_welcome()
        captured = capsys.readouterr()
        assert "neut chat" in captured.out
        assert "/help" in captured.out

    def test_welcome_with_gateway_stub(self, capsys, tmp_path):
        from unittest.mock import patch

        from axiom.infra.gateway import Gateway

        config_dir = tmp_path / "config"
        config_dir.mkdir()
        (config_dir / "models.toml").write_text("[gateway]\n")
        with patch("socket.create_connection", side_effect=OSError("mocked")):
            gw = Gateway(config_dir=config_dir)

        render_welcome(gateway=gw)
        captured = capsys.readouterr()
        assert "stub mode" in captured.out


class TestRenderSessionList:
    """Test session list rendering."""

    def test_empty_list(self, capsys):
        render_session_list([])
        captured = capsys.readouterr()
        assert "No saved sessions" in captured.out

    def test_with_sessions(self, capsys):
        sessions = [
            {"id": "abc123", "messages": 5, "updated": "2026-02-19"},
            {"id": "def456", "messages": 12, "updated": "2026-02-18"},
        ]
        render_session_list(sessions)
        captured = capsys.readouterr()
        assert "abc123" in captured.out
        assert "def456" in captured.out
        assert "5 messages" in captured.out

    def test_renders_one_message_singular(self, capsys):
        """T3.7 — 1 message, not 1 messages."""
        sessions = [{"id": "s1", "messages": 1, "updated": ""}]
        render_session_list(sessions)
        captured = capsys.readouterr()
        assert "1 message" in captured.out
        assert "1 messages" not in captured.out


class TestRenderApprovalPrompt:
    """T3.4 — approval prompt width-aware + truncation."""

    def test_approval_prompt_truncates_long_param_values(self, capsys, monkeypatch):
        import shutil

        from axiom.extensions.builtins.chat.renderer import render_approval_prompt
        from axiom.infra.orchestrator.actions import Action

        monkeypatch.setattr(shutil, "get_terminal_size", lambda *a, **kw: shutil.os.terminal_size((80, 24)))

        action = Action(name="write_file", params={"content": "x" * 200})
        # Patch input so approval prompt exits immediately
        monkeypatch.setattr("builtins.input", lambda _: "r")
        render_approval_prompt(action)
        captured = capsys.readouterr()
        # Value truncated: no 200-char run
        assert "x" * 200 not in captured.out
        assert "[+" in captured.out  # truncation marker

    def test_approval_prompt_divider_scales_to_terminal_width(self, capsys, monkeypatch):
        import shutil

        from axiom.extensions.builtins.chat.renderer import render_approval_prompt
        from axiom.infra.orchestrator.actions import Action

        monkeypatch.setattr(shutil, "get_terminal_size", lambda *a, **kw: shutil.os.terminal_size((40, 24)))
        action = Action(name="write_file", params={})
        monkeypatch.setattr("builtins.input", lambda _: "r")
        render_approval_prompt(action)
        captured = capsys.readouterr()
        # Box-drawing top/bottom lines must fit within terminal width
        box_lines = [ln for ln in captured.out.splitlines() if "┌" in ln or "└" in ln]
        for ln in box_lines:
            assert len(ln) <= 40, f"box line too wide for 40-col terminal: {ln!r}"


class TestApprovalCornerFrame:
    """T4.10 — approval prompt uses box-drawing corner-frame, not triple-hyphens."""

    def test_uses_box_drawing_not_hyphens(self, capsys, monkeypatch):
        import shutil

        from axiom.extensions.builtins.chat.renderer import render_approval_prompt
        from axiom.infra.orchestrator.actions import Action

        monkeypatch.setattr(shutil, "get_terminal_size", lambda *a, **kw: shutil.os.terminal_size((80, 24)))
        action = Action(name="write_file", params={})
        monkeypatch.setattr("builtins.input", lambda _: "r")
        render_approval_prompt(action)
        captured = capsys.readouterr()
        assert "┌─" in captured.out
        assert "└─" in captured.out
        assert "--- Write operation ---" not in captured.out

    def test_box_top_contains_approve_label(self, capsys, monkeypatch):
        import shutil

        from axiom.extensions.builtins.chat.renderer import render_approval_prompt
        from axiom.infra.orchestrator.actions import Action

        monkeypatch.setattr(shutil, "get_terminal_size", lambda *a, **kw: shutil.os.terminal_size((80, 24)))
        action = Action(name="write_file", params={})
        monkeypatch.setattr("builtins.input", lambda _: "r")
        render_approval_prompt(action)
        captured = capsys.readouterr()
        top_line = next(ln for ln in captured.out.splitlines() if "┌" in ln)
        assert "Approve" in top_line

    def test_params_line_uses_left_rule(self, capsys, monkeypatch):
        import shutil

        from axiom.extensions.builtins.chat.renderer import render_approval_prompt
        from axiom.infra.orchestrator.actions import Action

        monkeypatch.setattr(shutil, "get_terminal_size", lambda *a, **kw: shutil.os.terminal_size((80, 24)))
        action = Action(name="write_file", params={"path": "out.txt"})
        monkeypatch.setattr("builtins.input", lambda _: "r")
        render_approval_prompt(action)
        captured = capsys.readouterr()
        assert "│" in captured.out


class TestCodeBlockChrome:
    """T4.8 — code blocks render with language header + left rule."""

    def test_code_block_renders_header_with_lang(self, capsys):
        """Streaming a ``` python fence emits the ┌─ python header line."""
        from axiom.infra.gateway import StreamChunk

        chunks = iter([
            StreamChunk(type="text", text="```python\n"),
            StreamChunk(type="text", text="x = 1\n"),
            StreamChunk(type="text", text="```\n"),
            StreamChunk(type="done"),
        ])
        stream_text(chunks)
        out = capsys.readouterr().out
        assert "┌─" in out, f"expected ┌─ header in: {out!r}"
        assert "python" in out

    def test_code_block_body_uses_left_rule_not_dim_body(self, capsys):
        """Body lines inside a code fence get '│ ' prefix, not plain DIM."""
        from axiom.infra.gateway import StreamChunk

        chunks = iter([
            StreamChunk(type="text", text="```\n"),
            StreamChunk(type="text", text="echo hi\n"),
            StreamChunk(type="text", text="```\n"),
            StreamChunk(type="done"),
        ])
        stream_text(chunks)
        out = capsys.readouterr().out
        assert "│" in out, f"expected left-rule │ in: {out!r}"
        assert "echo hi" in out


class TestYellowDiscipline:
    """T4.4 — YELLOW reserved for live attention only."""

    def test_skipped_action_uses_dim_not_yellow(self, capsys):
        from axiom.infra.orchestrator.actions import Action

        set_color_enabled(True)
        from axiom.extensions.builtins.chat.renderer import render_action_result

        action = Action(name="write_file", params={})
        action.reject("skipped")
        render_action_result(action)
        captured = capsys.readouterr()
        assert "\x1b[33m" not in captured.out  # no YELLOW
        assert "skipped" in captured.out
        set_color_enabled(False)

    def test_approval_rule_does_not_use_yellow(self, capsys, monkeypatch):
        import shutil

        from axiom.extensions.builtins.chat.renderer import render_approval_prompt
        from axiom.infra.orchestrator.actions import Action

        set_color_enabled(True)
        monkeypatch.setattr(shutil, "get_terminal_size", lambda *a, **kw: shutil.os.terminal_size((80, 24)))
        action = Action(name="write_file", params={})
        monkeypatch.setattr("builtins.input", lambda _: "r")
        render_approval_prompt(action)
        captured = capsys.readouterr()
        # divider lines should not be yellow
        for line in captured.out.splitlines():
            if set(line.strip()) <= {"-"}:
                assert "\x1b[33m" not in line, f"divider line uses yellow: {line!r}"
        set_color_enabled(False)


class TestFormatParams:
    """Test parameter formatting."""

    def test_empty_params(self):
        assert "no parameters" in _format_params({})

    def test_single_param(self):
        result = _format_params({"file": "test.md"})
        assert "file=test.md" in result

    def test_multiple_params(self):
        result = _format_params({"source": "docs/prds/prd_foo.md", "draft": True})
        assert "source=" in result
        assert "draft=" in result
