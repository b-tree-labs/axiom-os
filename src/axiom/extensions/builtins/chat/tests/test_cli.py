# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tests for the chat CLI — slash commands, REPL behavior."""

from unittest.mock import MagicMock, patch

import pytest

from axiom.extensions.builtins.chat.cli import _handle_slash_command
from axiom.extensions.builtins.chat.commands import (
    cmd_clear,
    cmd_context,
    cmd_doctor,
    cmd_help,
    cmd_model,
    cmd_new,
    cmd_resume,
    cmd_sessions,
    cmd_signal,
    cmd_status,
    find_close_command,
    get_slash_commands,
)
from axiom.setup.renderer import set_color_enabled


@pytest.fixture(autouse=True)
def disable_color():
    set_color_enabled(False)
    yield
    set_color_enabled(False)


class TestSlashCommands:
    """Test individual slash command functions."""

    def test_cmd_help(self):
        result = cmd_help()
        assert "/help" in result
        assert "/status" in result
        assert "/exit" in result
        assert "/sessions" in result
        assert "/sessions rename" in result
        assert "/sessions archive" in result
        assert "/resume" in result
        assert "/new" in result
        assert "/clear" in result
        assert "/compact" in result
        assert "/model" in result
        assert "/context" in result
        assert "/doctor" in result
        # Primary names shown, not old names
        assert "/signal brief" in result
        assert "/signal status" in result
        assert "/pub status" in result
        assert "/pub overview" in result
        # Old names should NOT appear in help
        assert "/sense" not in result
        assert "/doc " not in result  # trailing space to avoid matching /doctor

    def test_cmd_status(self):
        from axiom.extensions.builtins.chat.agent import ChatAgent
        from axiom.extensions.builtins.chat.usage import UsageTracker
        from axiom.infra.gateway import Gateway
        from axiom.infra.orchestrator.session import Session

        agent = MagicMock(spec=ChatAgent)
        agent.session = Session()
        agent.session.add_message("user", "test")
        agent.gateway = MagicMock(spec=Gateway)
        agent.gateway.active_provider = None
        agent.usage = UsageTracker()

        result = cmd_status(agent)
        assert "Session:" in result
        assert "Messages:" in result
        assert "stub mode" in result

    def test_cmd_status_with_provider(self):
        from axiom.extensions.builtins.chat.agent import ChatAgent
        from axiom.extensions.builtins.chat.usage import UsageTracker
        from axiom.infra.gateway import Gateway
        from axiom.infra.orchestrator.session import Session

        agent = MagicMock(spec=ChatAgent)
        agent.session = Session()
        agent.gateway = MagicMock(spec=Gateway)
        agent.gateway._provider_override = None
        agent.gateway._model_override = None
        agent.gateway.providers = []
        agent.usage = UsageTracker()
        agent._session_mode = "auto"
        provider_mock = MagicMock()
        provider_mock.name = "anthropic"
        provider_mock.model = "claude-sonnet"
        provider_mock.routing_tier = "public"
        provider_mock.priority = 1
        provider_mock.requires_vpn = False
        agent.gateway.active_provider = provider_mock

        result = cmd_status(agent)
        assert "anthropic" in result
        assert "claude-sonnet" in result
        assert "Provider:" in result
        assert "Model:" in result
        assert "Tier:" in result
        assert "public" in result

    def test_cmd_status_shows_routing_details(self):
        """Status shows fallback chain, override reason, and VPN status."""
        from axiom.extensions.builtins.chat.agent import ChatAgent
        from axiom.extensions.builtins.chat.usage import UsageTracker
        from axiom.infra.gateway import Gateway
        from axiom.infra.orchestrator.session import Session

        agent = MagicMock(spec=ChatAgent)
        agent.session = Session()
        agent.gateway = MagicMock(spec=Gateway)
        agent.gateway._provider_override = "anthropic"
        agent.gateway._model_override = None
        agent._session_mode = "auto"
        agent.usage = UsageTracker()

        primary = MagicMock()
        primary.name = "anthropic"
        primary.model = "claude-sonnet"
        primary.routing_tier = "public"
        primary.priority = 1
        primary.requires_vpn = False
        primary.api_key = "sk-test"
        primary.api_key_env = "ANTHROPIC_API_KEY"
        agent.gateway.active_provider = primary

        fallback = MagicMock()
        fallback.name = "local-llm"
        fallback.model = "llama3"
        fallback.priority = 10
        fallback.requires_vpn = True
        fallback.api_key = ""
        fallback.api_key_env = ""
        agent.gateway.providers = [primary, fallback]

        result = cmd_status(agent)
        assert "user override" in result
        assert "Fallback:" in result
        assert "local-llm" in result
        assert "[vpn]" in result

    def test_cmd_signal(self):
        result = cmd_signal()
        assert "Signal" in result

    def test_cmd_sessions_empty(self):
        from axiom.infra.orchestrator.session import SessionStore

        store = MagicMock(spec=SessionStore)
        store.list_sessions.return_value = []

        result = cmd_sessions(store)
        assert "No saved sessions" in result

    def test_cmd_sessions_with_data(self):
        from axiom.infra.orchestrator.session import Session, SessionStore

        store = MagicMock(spec=SessionStore)
        store.list_sessions.return_value = ["abc123", "def456"]

        session1 = Session(session_id="abc123")
        session1.add_message("user", "test")
        session2 = Session(session_id="def456")
        store.load.side_effect = [session1, session2]

        result = cmd_sessions(store)
        assert "abc123" in result
        assert "def456" in result

    def test_cmd_resume_found(self):
        from axiom.extensions.builtins.chat.agent import ChatAgent
        from axiom.infra.orchestrator.session import Session, SessionStore

        store = MagicMock(spec=SessionStore)
        session = Session(session_id="abc123")
        session.add_message("user", "old message")
        store.load.return_value = session

        agent = MagicMock(spec=ChatAgent)

        result = cmd_resume("abc123", store, agent)
        assert "Resumed" in result
        assert "abc123" in result
        assert agent.session == session

    def test_cmd_resume_not_found(self):
        from axiom.extensions.builtins.chat.agent import ChatAgent
        from axiom.infra.orchestrator.session import SessionStore

        store = MagicMock(spec=SessionStore)
        store.load.return_value = None
        agent = MagicMock(spec=ChatAgent)

        result = cmd_resume("nonexistent", store, agent)
        assert "not found" in result.lower()

    def test_cmd_new(self):
        from axiom.extensions.builtins.chat.agent import ChatAgent
        from axiom.infra.orchestrator.session import Session, SessionStore

        store = MagicMock(spec=SessionStore)
        new_session = Session()
        store.create.return_value = new_session

        agent = MagicMock(spec=ChatAgent)
        agent.session = Session(session_id="old_id")

        result = cmd_new(store, agent)
        assert "Saved" in result or "started" in result
        assert agent.session == new_session


class TestSlashCommandDispatch:
    """Test the dispatch table in CLI."""

    def test_all_commands_documented(self):
        """All commands in get_slash_commands() should exist."""
        all_commands = get_slash_commands()
        assert "/help" in all_commands
        assert "/status" in all_commands
        assert "/exit" in all_commands
        assert "/sessions" in all_commands
        assert "/sessions rename" in all_commands
        assert "/sessions archive" in all_commands
        assert "/clear" in all_commands
        assert "/compact" in all_commands
        assert "/model" in all_commands
        assert "/context" in all_commands
        assert "/doctor" in all_commands
        # CLI commands are auto-synced under new primary names
        assert any("/signal" in cmd for cmd in all_commands)

    def test_dispatch_help(self):
        agent = MagicMock()
        store = MagicMock()
        result = _handle_slash_command("/help", agent, store)
        assert result is not None
        assert "/help" in result
        assert result != "exit"

    def test_dispatch_exit(self, capsys):
        agent = MagicMock()
        store = MagicMock()
        result = _handle_slash_command("/exit", agent, store)
        assert result == "exit"

    def test_dispatch_unknown(self):
        agent = MagicMock()
        store = MagicMock()
        result = _handle_slash_command("/unknown_xyz", agent, store)
        assert result is not None
        assert "Unknown command" in result

    def test_dispatch_resume_no_arg(self):
        agent = MagicMock()
        store = MagicMock()
        result = _handle_slash_command("/resume", agent, store)
        assert result is not None
        assert "Usage" in result

    def test_dispatch_resume_with_arg(self):
        from axiom.infra.orchestrator.session import Session

        agent = MagicMock()
        store = MagicMock()
        session = Session(session_id="test_id")
        store.load.return_value = session

        result = _handle_slash_command("/resume test_id", agent, store)
        assert result is not None
        assert "Resumed" in result


class TestSessionsSubcommands:
    """Test /sessions rename and /sessions archive subcommand dispatch."""

    def test_dispatch_sessions_rename(self):
        from axiom.extensions.builtins.chat.agent import ChatAgent
        from axiom.infra.orchestrator.session import Session, SessionStore

        agent = MagicMock(spec=ChatAgent)
        agent.session = Session(session_id="abc123")
        store = MagicMock(spec=SessionStore)

        result = _handle_slash_command("/sessions rename My Title", agent, store)
        assert result is not None
        assert "My Title" in result
        assert agent.session.title == "My Title"

    def test_dispatch_sessions_archive(self):
        from axiom.extensions.builtins.chat.agent import ChatAgent
        from axiom.infra.orchestrator.session import Session, SessionStore

        agent = MagicMock(spec=ChatAgent)
        agent.session = Session(session_id="current_id")
        store = MagicMock(spec=SessionStore)
        store.list_sessions.return_value = ["abc123", "def456"]
        store.archive.return_value = True

        result = _handle_slash_command("/sessions archive abc123", agent, store)
        assert result is not None
        assert "Archived" in result or "archived" in result.lower()
        store.archive.assert_called_with("abc123")

    def test_dispatch_sessions_unknown_sub(self):
        agent = MagicMock()
        store = MagicMock()
        result = _handle_slash_command("/sessions foobar", agent, store)
        assert result is not None
        assert "Unknown" in result
        assert "foobar" in result

    def test_rename_backward_compat(self):
        from axiom.extensions.builtins.chat.agent import ChatAgent
        from axiom.infra.orchestrator.session import Session, SessionStore

        agent = MagicMock(spec=ChatAgent)
        agent.session = Session(session_id="abc123")
        store = MagicMock(spec=SessionStore)

        result = _handle_slash_command("/rename My Title", agent, store)
        assert result is not None
        assert "My Title" in result
        assert agent.session.title == "My Title"

    def test_archive_backward_compat(self):
        from axiom.extensions.builtins.chat.agent import ChatAgent
        from axiom.infra.orchestrator.session import Session, SessionStore

        agent = MagicMock(spec=ChatAgent)
        agent.session = Session(session_id="current_id")
        store = MagicMock(spec=SessionStore)
        store.archive.return_value = True
        new_session = Session()
        store.create.return_value = new_session

        result = _handle_slash_command("/archive", agent, store)
        assert result is not None
        assert "Archived" in result or "archived" in result.lower()

    def test_sessions_subcommands_in_registry(self):
        """Both /sessions rename and /sessions archive should be in get_slash_commands()."""
        all_commands = get_slash_commands()
        assert "/sessions rename" in all_commands
        assert "/sessions archive" in all_commands

    def test_bare_sessions_lists(self):
        from axiom.infra.orchestrator.session import SessionStore

        store = MagicMock(spec=SessionStore)
        store.list_sessions.return_value = []
        agent = MagicMock()

        result = _handle_slash_command("/sessions", agent, store)
        assert result is not None
        assert "No saved sessions" in result


class TestBannerRendering:
    """Test that the salamander banner shows when show_banner=True."""

    def test_render_welcome_with_banner(self, capsys):
        from axiom.extensions.builtins.chat.providers.ansi_render import (
            AnsiRenderProvider,
        )

        p = AnsiRenderProvider()
        p.render_welcome(show_banner=True)
        captured = capsys.readouterr()
        assert "AXI" in captured.out

    def test_render_welcome_without_banner(self, capsys):
        from axiom.extensions.builtins.chat.providers.ansi_render import (
            AnsiRenderProvider,
        )

        p = AnsiRenderProvider()
        p.render_welcome(show_banner=False)
        captured = capsys.readouterr()
        assert "N E U T R O N  O S" not in captured.out
        assert "axi chat" in captured.out

    def test_bare_flag_in_parser(self):
        """--bare flag exists but is suppressed from help."""
        from axiom.extensions.builtins.chat.cli import get_parser

        parser = get_parser()
        args = parser.parse_args(["--bare"])
        assert args.bare is True

    def test_bare_flag_default_false(self):
        from axiom.extensions.builtins.chat.cli import get_parser

        parser = get_parser()
        args = parser.parse_args([])
        assert args.bare is False


class TestFindCloseCommand:
    """Test the fuzzy command matching helper."""

    def test_close_match_found(self):
        """A near-miss like /sesions should match /sessions."""
        result = find_close_command("/sesions")
        assert result == "/sessions"

    def test_close_match_help(self):
        """A near-miss like /helo should match /help."""
        result = find_close_command("/helo")
        assert result == "/help"

    def test_no_match_for_garbage(self):
        """A completely unrelated string returns None."""
        result = find_close_command("/xyzzy_garbage_999")
        assert result is None

    def test_exact_match_returns_itself(self):
        """An exact command name should be returned as a match."""
        result = find_close_command("/help")
        assert result == "/help"

    def test_multi_word_uses_first_word(self):
        """Only the first word is used for matching."""
        result = find_close_command("/statu extra args")
        assert result == "/status"


class TestRenamedCommands:
    """Test /pub and /signal dispatch, plus /doc and /sense hidden aliases."""

    def test_pub_dispatches(self):
        """'/pub' should dispatch to CLI command handler."""
        agent = MagicMock()
        store = MagicMock()
        result = _handle_slash_command("/pub", agent, store)
        assert result is not None
        # Should show doc status (the /pub bare command shows doc status)
        assert result != "exit"

    def test_signal_dispatches(self):
        """'/signal' should dispatch to CLI command handler."""
        agent = MagicMock()
        store = MagicMock()
        result = _handle_slash_command("/signal", agent, store)
        assert result is not None
        assert result != "exit"

    def test_doc_hidden_alias(self):
        """'/doc' still works as hidden alias for /pub."""
        agent = MagicMock()
        store = MagicMock()
        result = _handle_slash_command("/doc", agent, store)
        assert result is not None
        assert result != "exit"

    def test_sense_hidden_alias(self):
        """'/sense' still works as hidden alias for /signal."""
        agent = MagicMock()
        store = MagicMock()
        result = _handle_slash_command("/sense", agent, store)
        assert result is not None
        assert result != "exit"


class TestNewCommands:
    """Test /clear, /model, /context, /doctor."""

    def _make_agent(self):
        from axiom.extensions.builtins.chat.agent import ChatAgent
        from axiom.extensions.builtins.chat.usage import UsageTracker
        from axiom.infra.gateway import Gateway
        from axiom.infra.orchestrator.session import Session

        agent = MagicMock(spec=ChatAgent)
        agent.session = Session()
        agent.session.add_message("user", "hello")
        agent.session.add_message("assistant", "hi there")
        agent.gateway = MagicMock(spec=Gateway)
        agent.gateway.available = True
        agent.gateway.providers = []
        provider_mock = MagicMock()
        provider_mock.name = "anthropic"
        provider_mock.model = "claude-sonnet"
        agent.gateway.active_provider = provider_mock
        agent.usage = UsageTracker()
        return agent

    def test_clear_clears_messages(self):
        agent = self._make_agent()
        assert len(agent.session.messages) == 2
        result = cmd_clear(agent)
        assert "cleared" in result.lower()
        assert len(agent.session.messages) == 0

    def test_clear_dispatch(self):
        agent = self._make_agent()
        store = MagicMock()
        result = _handle_slash_command("/clear", agent, store)
        assert result is not None
        assert "cleared" in result.lower()

    def test_model_shows_providers(self):
        agent = self._make_agent()
        # Add provider to list so it appears
        agent.gateway.providers = [agent.gateway.active_provider]
        result = cmd_model(agent)
        assert "anthropic" in result

    def test_model_dispatch_no_arg(self):
        agent = self._make_agent()
        store = MagicMock()
        result = _handle_slash_command("/model", agent, store)
        assert result is not None
        assert "provider" in result.lower() or "anthropic" in result.lower()

    def test_context_shows_token_count(self):
        agent = self._make_agent()
        result = cmd_context(agent)
        assert "Est. tokens" in result
        assert "Usage:" in result

    def test_context_renders_progress_bar(self):
        """T3.9 — /context output contains a Unicode progress bar."""
        agent = self._make_agent()
        result = cmd_context(agent)
        assert "█" in result or "░" in result, "progress bar glyphs missing"

    def test_context_dispatch(self):
        agent = self._make_agent()
        store = MagicMock()
        result = _handle_slash_command("/context", agent, store)
        assert result is not None
        assert "token" in result.lower() or "Usage" in result

    def test_doctor_shows_health(self):
        agent = self._make_agent()
        result = cmd_doctor(agent)
        assert "Health" in result
        assert "Gateway" in result

    def test_doctor_unavailable_gateway_includes_remediation_hint(self):
        """T3.10 — when gateway is UNAVAILABLE, hint appears."""
        from axiom.extensions.builtins.chat.usage import UsageTracker
        from axiom.infra.gateway import Gateway
        from axiom.infra.orchestrator.session import Session

        agent = MagicMock()
        agent.session = Session()
        gw = MagicMock(spec=Gateway)
        gw.available = False
        gw.active_provider = None
        agent.gateway = gw
        agent.usage = UsageTracker()

        result = cmd_doctor(agent)
        assert "UNAVAILABLE" in result
        assert "axi config" in result or "ANTHROPIC_API_KEY" in result

    def test_doctor_dispatch(self):
        agent = self._make_agent()
        store = MagicMock()
        result = _handle_slash_command("/doctor", agent, store)
        assert result is not None
        assert "Health" in result

    def test_cmd_status_labels_align(self):
        """T3.8 — all label columns start at the same horizontal position."""
        import re

        from axiom.extensions.builtins.chat.usage import UsageTracker
        from axiom.infra.gateway import Gateway
        from axiom.infra.orchestrator.session import Session

        agent = MagicMock()
        agent.session = Session()
        agent.session.add_message("user", "test")
        agent.gateway = MagicMock(spec=Gateway)
        agent.gateway._provider_override = None
        agent.gateway._model_override = None
        agent.gateway.providers = []
        agent.usage = UsageTracker()
        agent._session_mode = "auto"
        provider = MagicMock()
        provider.name = "anthropic"
        provider.model = "claude-sonnet"
        provider.routing_tier = "public"
        provider.priority = 1
        provider.requires_vpn = False
        agent.gateway.active_provider = provider

        result = cmd_status(agent)
        plain = re.compile(r"\x1b\[[0-9;]*m").sub("", result)
        label_lines = [
            ln for ln in plain.splitlines()
            if ln.startswith("  ") and ":" in ln and not ln.strip().startswith("#")
        ]
        assert len(label_lines) >= 2, "too few label lines to test alignment"

        def _value_col(ln: str) -> int:
            stripped = ln.lstrip(" ")
            label_end = stripped.index(":") + 1
            rest = stripped[label_end:]
            return len(ln) - len(stripped) + label_end + (len(rest) - len(rest.lstrip()))

        cols = [_value_col(ln) for ln in label_lines]
        assert len(set(cols)) == 1, f"labels not aligned: {cols} from {label_lines}"


class TestMultiLineInput:
    """Test multi-line input parsing (conceptual)."""

    def test_triple_quote_toggling(self):
        """Verify triple-quote detection works."""
        assert '"""'.strip() == '"""'
        assert '"""'.strip() == '"""'

    def test_multiline_buffer_joining(self):
        """Multi-line buffer joins with newlines."""
        buffer = ["line 1", "line 2", "line 3"]
        result = "\n".join(buffer)
        assert result == "line 1\nline 2\nline 3"


class TestFirstRunGuard:
    """Tests for build_setup_hint — first-run no-provider guard."""

    def _make_gateway(self, providers):
        """Build a MagicMock(spec=Gateway) mirroring real available semantics."""
        from axiom.infra.gateway import Gateway

        gw = MagicMock(spec=Gateway)
        gw.providers = providers
        # Mirror real available: any(p.api_key or not p.api_key_env for p in providers)
        gw.available = any(p.api_key or not p.api_key_env for p in providers)
        return gw

    def _make_provider(self, name, api_key, api_key_env):
        p = MagicMock()
        p.name = name
        p.api_key = api_key
        p.api_key_env = api_key_env
        return p

    def test_no_providers_returns_hint(self):
        """No providers configured → hint lists ANTHROPIC_API_KEY + config."""
        from axiom.extensions.builtins.chat.cli import build_setup_hint

        gw = self._make_gateway([])
        hint = build_setup_hint(gw)
        assert hint is not None
        assert "ANTHROPIC_API_KEY" in hint

    def test_provider_with_empty_key_returns_hint(self):
        """Provider configured but api_key env var not set → hint lists the var."""
        from axiom.extensions.builtins.chat.cli import build_setup_hint

        p = self._make_provider("anthropic", api_key=None, api_key_env="ANTHROPIC_API_KEY")
        gw = self._make_gateway([p])
        hint = build_setup_hint(gw)
        assert hint is not None
        assert "ANTHROPIC_API_KEY" in hint
        assert "export" in hint.lower() or "ANTHROPIC_API_KEY" in hint

    def test_provider_with_valid_key_returns_none(self):
        """Provider configured and api_key is set → no hint needed."""
        from axiom.extensions.builtins.chat.cli import build_setup_hint

        p = self._make_provider("anthropic", api_key="sk-test", api_key_env="ANTHROPIC_API_KEY")
        gw = self._make_gateway([p])
        hint = build_setup_hint(gw)
        assert hint is None

    def test_local_provider_empty_api_key_env_returns_none(self):
        """Local provider (empty api_key_env) is usable without a key → no hint."""
        from axiom.extensions.builtins.chat.cli import build_setup_hint

        p = self._make_provider("ollama", api_key=None, api_key_env="")
        gw = self._make_gateway([p])
        hint = build_setup_hint(gw)
        assert hint is None

    def test_hint_includes_brand_config_command(self):
        """Hint includes `{cli_name} config` when env vars are missing."""
        from axiom.extensions.builtins.chat.cli import build_setup_hint
        from axiom.infra.branding import get_branding

        p = self._make_provider("openai", api_key=None, api_key_env="OPENAI_API_KEY")
        gw = self._make_gateway([p])
        hint = build_setup_hint(gw)
        assert hint is not None
        cli_name = get_branding().cli_name
        assert cli_name in hint

    def test_no_providers_hint_includes_free_local_options_first(self):
        """No providers → menu lists local llamafile options BEFORE cloud keys.

        Bug #1 from Austin's 2026-05-19 onboarding pass: the previous
        narrow menu defaulted users toward paid commercial APIs
        (ANTHROPIC_API_KEY first). The new menu surfaces the free local
        path (llamafile via `{cli} config`) as the recommended option
        and lists cloud-key options as fallbacks.
        """
        from axiom.extensions.builtins.chat.cli import build_setup_hint
        from axiom.infra.branding import get_branding

        gw = self._make_gateway([])
        hint = build_setup_hint(gw)
        assert hint is not None
        cli_name = get_branding().cli_name

        # Both free local options surface.
        assert f"{cli_name} config --model bonsai" in hint, (
            "Free local bonsai option should appear in the no-providers hint"
        )
        assert f"{cli_name} config --model qwen" in hint, (
            "Free local qwen option should appear in the no-providers hint"
        )
        # Cloud options also present.
        assert "ANTHROPIC_API_KEY" in hint
        assert "OPENAI_API_KEY" in hint, (
            "OpenAI option should be listed alongside Anthropic; was previously missing"
        )
        # Free options listed BEFORE cloud keys (free-first ordering).
        bonsai_pos = hint.index(f"{cli_name} config --model bonsai")
        anthropic_pos = hint.index("ANTHROPIC_API_KEY")
        assert bonsai_pos < anthropic_pos, (
            "Free local options must be listed before cloud key options"
        )


class TestMaybeStartLocalLLM:
    """Tests for `maybe_start_local_llm` — auto-start of provisioned llamafile."""

    def _gateway(self, available: bool):
        from axiom.infra.gateway import Gateway

        gw = MagicMock(spec=Gateway)
        gw.available = available
        gw.providers = []
        # _discover_local_llm is the private re-probe method maybe_start
        # calls after starting the binary; track its calls.
        gw._discover_local_llm = MagicMock()
        return gw

    def test_no_op_when_gateway_already_available(self):
        """If a provider is already usable, we never touch llamafile."""
        from axiom.extensions.builtins.chat.cli import maybe_start_local_llm

        gw = self._gateway(available=True)
        with patch("axiom.setup.llamafile.is_llamafile_installed") as is_inst:
            transitioned = maybe_start_local_llm(gw)
        assert transitioned is False
        is_inst.assert_not_called()
        gw._discover_local_llm.assert_not_called()

    def test_no_op_when_llamafile_not_provisioned(self):
        """First-run with nothing downloaded → leave alone (no surprise dl)."""
        from axiom.extensions.builtins.chat.cli import maybe_start_local_llm

        gw = self._gateway(available=False)
        with patch(
            "axiom.setup.llamafile.is_llamafile_installed", return_value=False
        ):
            transitioned = maybe_start_local_llm(gw)
        assert transitioned is False
        gw._discover_local_llm.assert_not_called()

    def test_starts_provisioned_but_idle_llamafile(self):
        """Provisioned but not running → start + re-probe gateway."""
        from axiom.extensions.builtins.chat.cli import maybe_start_local_llm

        gw = self._gateway(available=False)

        def _flip_available():
            gw.available = True

        gw._discover_local_llm.side_effect = _flip_available

        with (
            patch(
                "axiom.setup.llamafile.is_llamafile_installed", return_value=True
            ),
            patch(
                "axiom.setup.llamafile.is_llamafile_running", return_value=False
            ),
            patch(
                "axiom.setup.llamafile.start_llamafile", return_value=True
            ) as start,
        ):
            transitioned = maybe_start_local_llm(gw)

        start.assert_called_once()
        gw._discover_local_llm.assert_called_once()
        assert transitioned is True

    def test_already_running_just_reprobes(self):
        """Llamafile already running → don't start, just nudge discovery."""
        from axiom.extensions.builtins.chat.cli import maybe_start_local_llm

        gw = self._gateway(available=False)

        def _flip_available():
            gw.available = True

        gw._discover_local_llm.side_effect = _flip_available

        with (
            patch(
                "axiom.setup.llamafile.is_llamafile_installed", return_value=True
            ),
            patch(
                "axiom.setup.llamafile.is_llamafile_running", return_value=True
            ),
            patch("axiom.setup.llamafile.start_llamafile") as start,
        ):
            transitioned = maybe_start_local_llm(gw)

        start.assert_not_called()
        gw._discover_local_llm.assert_called_once()
        assert transitioned is True

    def test_start_failure_falls_through_silently(self):
        """If start_llamafile fails, return False (caller shows the hint)."""
        from axiom.extensions.builtins.chat.cli import maybe_start_local_llm

        gw = self._gateway(available=False)
        with (
            patch(
                "axiom.setup.llamafile.is_llamafile_installed", return_value=True
            ),
            patch(
                "axiom.setup.llamafile.is_llamafile_running", return_value=False
            ),
            patch(
                "axiom.setup.llamafile.start_llamafile", return_value=False
            ),
        ):
            transitioned = maybe_start_local_llm(gw)
        assert transitioned is False


# ---------------------------------------------------------------------------
# T4.3 — blank-line contract via surface_block()
# ---------------------------------------------------------------------------


class TestSurfaceBlockContract:
    """T4.3 — each command surface has 1 leading blank, 0 trailing."""

    def test_cmd_help_starts_with_one_blank_line(self):
        result = cmd_help()
        assert result.startswith("\n")
        assert not result.startswith("\n\n")

    def test_cmd_help_does_not_end_with_blank_line(self):
        result = cmd_help()
        assert not result.endswith("\n")

    def test_cmd_help_followed_by_prompt_has_at_most_one_blank_between_sections(self):
        """Simulate the REPL appending '> ' after the command output."""
        combined = cmd_help() + "\n> "
        consecutive_blanks = 0
        max_consecutive = 0
        for line in combined.splitlines():
            if line.strip() == "":
                consecutive_blanks += 1
                max_consecutive = max(max_consecutive, consecutive_blanks)
            else:
                consecutive_blanks = 0
        assert max_consecutive <= 2, f"too many consecutive blank lines: {max_consecutive}"

    def test_cmd_model_surface_block_contract(self):
        from unittest.mock import MagicMock

        from axiom.extensions.builtins.chat.commands import cmd_model
        agent = MagicMock()
        agent.gateway.providers = []
        agent.gateway.active_provider = None
        result = cmd_model(agent)
        assert result.startswith("\n")
        assert not result.endswith("\n")
