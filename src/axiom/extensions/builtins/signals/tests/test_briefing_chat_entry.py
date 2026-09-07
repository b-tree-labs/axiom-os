# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tests for briefing → chat transition (enter_chat + enticement)."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import patch

import pytest

from axiom.extensions.builtins.chat.entry import _format_briefing_context, enter_chat
from axiom.infra.orchestrator.session import Session

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _sample_briefing_data() -> dict:
    """Return a realistic Briefing.to_dict() payload."""
    return {
        "briefing_id": "abc123",
        "generated_at": datetime.now(UTC).isoformat(),
        "time_window_start": "2026-02-25T08:00:00+00:00",
        "time_window_end": "2026-02-26T08:00:00+00:00",
        "summary": "Three key developments this week: Alpha control system upgrade "
                   "completed, new safety protocol drafted, and simulation pipeline "
                   "reached v0.3 milestone.",
        "signal_count": 12,
        "signals_by_type": {"gitlab": 5, "voice": 4, "freetext": 3},
        "key_signals": [
            {"signal_type": "gitlab", "detail": "Merged MR !42: control rod calibration"},
            {"signal_type": "voice", "detail": "Kevin mentioned INL meeting rescheduled"},
        ],
        "topic": "general",
        "topic_query": "",
        "confidence": 0.85,
        "time_window_reason": "Since last briefing",
    }


# ---------------------------------------------------------------------------
# _format_briefing_context
# ---------------------------------------------------------------------------

class TestFormatBriefingContext:
    def test_contains_summary(self):
        data = _sample_briefing_data()
        md = _format_briefing_context(data)
        assert "Three key developments" in md

    def test_contains_signal_count(self):
        data = _sample_briefing_data()
        md = _format_briefing_context(data)
        assert "12" in md

    def test_contains_key_signals(self):
        data = _sample_briefing_data()
        md = _format_briefing_context(data)
        assert "control rod calibration" in md
        assert "INL meeting rescheduled" in md

    def test_contains_confidence(self):
        data = _sample_briefing_data()
        md = _format_briefing_context(data)
        assert "85%" in md

    def test_contains_breakdown(self):
        data = _sample_briefing_data()
        md = _format_briefing_context(data)
        assert "gitlab" in md
        assert "voice" in md

    def test_topic_label_shown_for_non_general(self):
        data = _sample_briefing_data()
        data["topic"] = "blockers"
        data["topic_query"] = "blockers"
        md = _format_briefing_context(data)
        assert "**Topic:** blockers" in md

    def test_topic_label_hidden_for_general(self):
        data = _sample_briefing_data()
        md = _format_briefing_context(data)
        assert "**Topic:**" not in md

    def test_topic_with_distinct_query(self):
        data = _sample_briefing_data()
        data["topic"] = "people"
        data["topic_query"] = "Kevin"
        md = _format_briefing_context(data)
        assert "people (Kevin)" in md

    def test_empty_briefing(self):
        md = _format_briefing_context({})
        assert "Executive Briefing" in md
        assert "0%" in md  # default confidence


# ---------------------------------------------------------------------------
# enter_chat — actually calls the function
# ---------------------------------------------------------------------------

class TestEnterChat:
    """Test enter_chat() by mocking its dependencies at module level."""

    @patch("axiom.extensions.builtins.chat.entry._is_tty", return_value=False)
    @patch("axiom.extensions.builtins.chat.entry.run_repl")
    @patch("axiom.extensions.builtins.chat.entry.create_input_provider")
    @patch("axiom.extensions.builtins.chat.entry.create_render_provider")
    @patch("axiom.extensions.builtins.chat.entry.Gateway")
    @patch("axiom.extensions.builtins.chat.entry.EventBus")
    @patch("axiom.extensions.builtins.chat.entry.SessionStore")
    def test_session_created_with_context(
        self, MockStore, MockBus, MockGw, mock_rp, mock_ip, mock_repl, mock_tty,
    ):
        session = Session()
        mock_store = MockStore.return_value
        mock_store.create.return_value = session

        enter_chat(
            context_markdown="# Test briefing",
            context_data={"signal_count": 5},
            title="Briefing: test",
            source="neut_sense_brief",
        )

        # Verify store.create was called with the right context dict
        mock_store.create.assert_called_once()
        ctx = mock_store.create.call_args[1]["context"]
        assert ctx["context_markdown"] == "# Test briefing"
        assert ctx["context_data"] == {"signal_count": 5}
        assert ctx["source"] == "neut_sense_brief"

    @patch("axiom.extensions.builtins.chat.entry._is_tty", return_value=False)
    @patch("axiom.extensions.builtins.chat.entry.run_repl")
    @patch("axiom.extensions.builtins.chat.entry.create_input_provider")
    @patch("axiom.extensions.builtins.chat.entry.create_render_provider")
    @patch("axiom.extensions.builtins.chat.entry.Gateway")
    @patch("axiom.extensions.builtins.chat.entry.EventBus")
    @patch("axiom.extensions.builtins.chat.entry.SessionStore")
    def test_title_set_on_session(
        self, MockStore, MockBus, MockGw, mock_rp, mock_ip, mock_repl, mock_tty,
    ):
        session = Session()
        MockStore.return_value.create.return_value = session

        enter_chat(context_markdown="# X", title="Briefing: blockers")

        assert session.title == "Briefing: blockers"

    @patch("axiom.extensions.builtins.chat.entry._is_tty", return_value=False)
    @patch("axiom.extensions.builtins.chat.entry.run_repl")
    @patch("axiom.extensions.builtins.chat.entry.create_input_provider")
    @patch("axiom.extensions.builtins.chat.entry.create_render_provider")
    @patch("axiom.extensions.builtins.chat.entry.Gateway")
    @patch("axiom.extensions.builtins.chat.entry.EventBus")
    @patch("axiom.extensions.builtins.chat.entry.SessionStore")
    def test_repl_fallback_when_no_tty(
        self, MockStore, MockBus, MockGw, mock_rp, mock_ip, mock_repl, mock_tty,
    ):
        MockStore.return_value.create.return_value = Session()

        enter_chat(context_markdown="# Test")

        mock_repl.assert_called_once()

    @patch("axiom.extensions.builtins.chat.entry._is_tty", return_value=False)
    @patch("axiom.extensions.builtins.chat.entry.run_repl")
    @patch("axiom.extensions.builtins.chat.entry.create_input_provider")
    @patch("axiom.extensions.builtins.chat.entry.create_render_provider")
    @patch("axiom.extensions.builtins.chat.entry.Gateway")
    @patch("axiom.extensions.builtins.chat.entry.EventBus")
    @patch("axiom.extensions.builtins.chat.entry.SessionStore")
    def test_session_saved_after_repl(
        self, MockStore, MockBus, MockGw, mock_rp, mock_ip, mock_repl, mock_tty,
    ):
        session = Session()
        mock_store = MockStore.return_value
        mock_store.create.return_value = session

        enter_chat(context_markdown="# Test")

        mock_store.save.assert_called_once_with(session)

    @patch("axiom.extensions.builtins.chat.entry._is_tty", return_value=False)
    @patch("axiom.extensions.builtins.chat.entry.run_repl", side_effect=RuntimeError("boom"))
    @patch("axiom.extensions.builtins.chat.entry.create_input_provider")
    @patch("axiom.extensions.builtins.chat.entry.create_render_provider")
    @patch("axiom.extensions.builtins.chat.entry.Gateway")
    @patch("axiom.extensions.builtins.chat.entry.EventBus")
    @patch("axiom.extensions.builtins.chat.entry.SessionStore")
    def test_session_saved_even_on_repl_crash(
        self, MockStore, MockBus, MockGw, mock_rp, mock_ip, mock_repl, mock_tty,
    ):
        session = Session()
        mock_store = MockStore.return_value
        mock_store.create.return_value = session

        with pytest.raises(RuntimeError):
            enter_chat(context_markdown="# Test")

        mock_store.save.assert_called_once_with(session)

    @patch("axiom.extensions.builtins.chat.entry._is_tty", return_value=False)
    @patch("axiom.extensions.builtins.chat.entry.run_repl")
    @patch("axiom.extensions.builtins.chat.entry.create_input_provider")
    @patch("axiom.extensions.builtins.chat.entry.create_render_provider")
    @patch("axiom.extensions.builtins.chat.entry.Gateway")
    @patch("axiom.extensions.builtins.chat.entry.EventBus")
    @patch("axiom.extensions.builtins.chat.entry.SessionStore")
    def test_empty_optionals_excluded_from_context(
        self, MockStore, MockBus, MockGw, mock_rp, mock_ip, mock_repl, mock_tty,
    ):
        MockStore.return_value.create.return_value = Session()

        enter_chat(context_markdown="# X", context_data=None, source="")

        ctx = MockStore.return_value.create.call_args[1]["context"]
        assert "context_markdown" in ctx
        assert "context_data" not in ctx
        assert "source" not in ctx

    @patch("axiom.extensions.builtins.chat.entry._is_tty", return_value=True)
    @patch("axiom.extensions.builtins.chat.entry.FullScreenChat")
    @patch("axiom.extensions.builtins.chat.entry.Gateway")
    @patch("axiom.extensions.builtins.chat.entry.EventBus")
    @patch("axiom.extensions.builtins.chat.entry.SessionStore")
    def test_fullscreen_used_when_tty(
        self, MockStore, MockBus, MockGw, MockTui, mock_tty,
    ):
        session = Session()
        mock_store = MockStore.return_value
        mock_store.create.return_value = session

        enter_chat(context_markdown="# Test")

        MockTui.assert_called_once()
        MockTui.return_value.run.assert_called_once()
        mock_store.save.assert_called_once()

    @patch("axiom.extensions.builtins.chat.entry._is_tty", return_value=True)
    @patch("axiom.extensions.builtins.chat.entry.FullScreenChat")
    @patch("axiom.extensions.builtins.chat.entry.Gateway")
    @patch("axiom.extensions.builtins.chat.entry.EventBus")
    @patch("axiom.extensions.builtins.chat.entry.SessionStore")
    def test_suggestions_injected_for_fullscreen(
        self, MockStore, MockBus, MockGw, MockTui, mock_tty,
    ):
        session = Session()
        MockStore.return_value.create.return_value = session

        suggestions = ["What are the key takeaways?", "Any blockers?"]
        enter_chat(context_markdown="# X", suggestions=suggestions)

        tui_instance = MockTui.return_value
        assert tui_instance._suggestion_key == "context"


# ---------------------------------------------------------------------------
# Enticement in cmd_brief — TTY guard
# ---------------------------------------------------------------------------

class TestEnticementTtyGuard:
    @patch("sys.stdin")
    @patch("sys.stdout")
    def test_enticement_skipped_when_piped(self, mock_stdout, mock_stdin, capsys):
        """The isatty guard should prevent the prompt when piped."""
        mock_stdin.isatty.return_value = False
        mock_stdout.isatty.return_value = False

        import sys
        assert not (sys.stdin.isatty() and sys.stdout.isatty())

    @patch("sys.stdin")
    @patch("sys.stdout")
    def test_enticement_shown_when_tty(self, mock_stdout, mock_stdin):
        mock_stdin.isatty.return_value = True
        mock_stdout.isatty.return_value = True

        import sys
        assert sys.stdin.isatty() and sys.stdout.isatty()


# ---------------------------------------------------------------------------
# System prompt injection
# ---------------------------------------------------------------------------

class TestSystemPromptInjection:
    def test_context_markdown_in_system_prompt(self):
        from axiom.extensions.builtins.chat.agent import ChatAgent

        session = Session(context={"context_markdown": "# My Briefing\nSome content"})
        agent = ChatAgent(session=session)

        prompt = agent._build_system_prompt()
        assert "My Briefing" in prompt
        assert "Context from terminal command" in prompt
        assert "reference" in prompt.lower()

    def test_no_context_markdown_no_injection(self):
        from axiom.extensions.builtins.chat.agent import ChatAgent

        session = Session(context={})
        agent = ChatAgent(session=session)

        prompt = agent._build_system_prompt()
        assert "Context from terminal command" not in prompt

    def test_context_markdown_truncated_at_6000_chars(self):
        from axiom.extensions.builtins.chat.agent import ChatAgent

        long_md = "x" * 10000
        session = Session(context={"context_markdown": long_md})
        agent = ChatAgent(session=session)

        prompt = agent._build_system_prompt()
        # The injected context should be at most 6000 chars of the markdown
        # plus the header text
        assert "x" * 6000 in prompt
        assert "x" * 6001 not in prompt

    def test_context_coexists_with_file_content(self):
        from axiom.extensions.builtins.chat.agent import ChatAgent

        session = Session(context={
            "file_content": "file stuff here",
            "context_markdown": "# Briefing stuff",
        })
        agent = ChatAgent(session=session)

        prompt = agent._build_system_prompt()
        assert "file stuff here" in prompt
        assert "Briefing stuff" in prompt
