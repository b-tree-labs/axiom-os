# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""The operator's own notes belong in the operator's prompt and nobody else's.

Three of the fragments ``_build_system_prompt`` composes are read from the
machine the process is running on rather than from the request:

* the repository's project file, ``<repo root>/CLAUDE.md``;
* the personal context file beside it, ``<repo root>/.claude/context.md``;
* the operator's prompt library, the ``*.md`` files under the user state
  directory and the project's dotted config directory.

At a terminal the operator and the person asking are the same person, so all
three are wanted and the terminal must keep getting them exactly as it always
has. On a serving worker they are different people, and every one of those
fragments would go into the system prompt of every request from every user.
That is a disclosure, and it is the same per-process versus per-request shape
as the approval leak the scope was built to fix.

The fix is a declared field on ``ChatScope``. These tests pin it:

* a headless prompt contains none of the three, asserted one source at a
  time, from a fixture where all three files exist and are non-empty (which
  the terminal half of the same fixture proves by finding them);
* a terminal prompt contains all three, under the same section headers;
* the exclusion holds on every request of a headless handle, not just the
  first, and on a scope reused across turns;
* the switch is declared by the surface. Nothing reads the environment,
  ``isatty`` or the presence of a file to decide it, and the same process
  environment yields both answers depending only on the declaration.
"""

from __future__ import annotations

import ast
import inspect
import textwrap
from unittest.mock import MagicMock

import pytest

from axiom.extensions.builtins.chat import agent as agent_mod
from axiom.extensions.builtins.chat.agent import ChatAgent
from axiom.extensions.builtins.chat.headless import HeadlessChat
from axiom.extensions.builtins.chat.scope import ChatScope
from axiom.infra.bus import EventBus
from axiom.infra.gateway import CompletionResponse, Gateway

# One marker per operator-local source, so every assertion names exactly which
# source it is about. An aggregate "nothing leaked" check would pass while two
# of the three were still being composed in.
PROJECT_FILE_MARKER = "OPERATOR-LOCAL-PROJECT-FILE-9f21"
PERSONAL_CONTEXT_MARKER = "OPERATOR-LOCAL-PERSONAL-CONTEXT-4c07"
PROMPT_LIBRARY_MARKER = "OPERATOR-LOCAL-PROMPT-LIBRARY-b3e8"

PROJECT_FILE_HEADER = "--- Project context (CLAUDE.md) ---"
PERSONAL_CONTEXT_HEADER = "--- Personal context ---"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_gateway():
    gw = MagicMock(spec=Gateway)
    gw.available = True
    gw.active_provider = MagicMock()
    gw.active_provider.name = "test"
    gw.active_provider.model = "test-model"
    gw.complete_with_tools.return_value = CompletionResponse(
        text="ok", provider="test", success=True
    )
    return gw


@pytest.fixture
def operator_machine(tmp_path, monkeypatch):
    """An operator's machine with all three local sources present and filled.

    The case that actually leaks is the one where these files exist and have
    content in them. A fixture that happened to have no project file would let
    every exclusion assertion below pass while the defect was untouched, so
    each file is written here and the terminal tests read every marker back
    out of a real composed prompt.
    """
    root = tmp_path / "operator-repo"
    (root / ".claude").mkdir(parents=True)
    (root / ".git").mkdir()
    (root / "CLAUDE.md").write_text(
        f"# Project notes\n{PROJECT_FILE_MARKER}\nInternal build steps.\n",
        encoding="utf-8",
    )
    (root / ".claude" / "context.md").write_text(
        f"# Who I am\n{PERSONAL_CONTEXT_MARKER}\nMy private working notes.\n",
        encoding="utf-8",
    )

    state = tmp_path / "operator-state"
    (state / "prompts").mkdir(parents=True)
    (state / "prompts" / "house_style.md").write_text(
        f"{PROMPT_LIBRARY_MARKER}\nAlways answer in the house voice.\n",
        encoding="utf-8",
    )

    monkeypatch.setattr(agent_mod, "_REPO_ROOT", root)
    monkeypatch.setenv("AXIOM_ROOT", str(root))
    monkeypatch.setenv("AXI_STATE_DIR", str(state))
    return root


@pytest.fixture
def terminal_agent(mock_gateway, tmp_path, operator_machine):
    """What an operator at a keyboard has always built: no scope, no policy."""
    return ChatAgent(
        gateway=mock_gateway,
        bus=EventBus(log_path=tmp_path / "terminal-events.jsonl"),
    )


@pytest.fixture
def headless(mock_gateway, tmp_path, operator_machine):
    """A serving surface's handle, on the same machine, same environment."""
    return HeadlessChat(
        turn_deadline=30.0,
        gateway=mock_gateway,
        bus=EventBus(log_path=tmp_path / "headless-events.jsonl"),
    )


def _headless_prompt(chat: HeadlessChat) -> str:
    """The system prompt one request of ``chat`` would be answered under."""
    return chat.agent._build_system_prompt(scope=chat.new_scope())


def _systems_sent(gateway) -> list[str]:
    """Every system prompt the gateway was actually called with, in order."""
    return [call.kwargs["system"] for call in gateway.complete_with_tools.call_args_list]


def _prompt_builder_ast() -> ast.AST:
    """``_build_system_prompt`` as parsed code, with comments and text gone."""
    return ast.parse(textwrap.dedent(inspect.getsource(ChatAgent._build_system_prompt)))


# ---------------------------------------------------------------------------
# A headless surface excludes every operator-local source
# ---------------------------------------------------------------------------


class TestHeadlessExcludesOperatorLocalSources:
    """Asserted one source at a time: an aggregate check hides two of three."""

    def test_headless_prompt_omits_the_project_file(self, headless):
        prompt = _headless_prompt(headless)
        assert PROJECT_FILE_MARKER not in prompt
        assert PROJECT_FILE_HEADER not in prompt

    def test_headless_prompt_omits_the_personal_context_file(self, headless):
        prompt = _headless_prompt(headless)
        assert PERSONAL_CONTEXT_MARKER not in prompt
        assert PERSONAL_CONTEXT_HEADER not in prompt

    def test_headless_prompt_omits_the_operator_prompt_library(self, headless):
        prompt = _headless_prompt(headless)
        assert PROMPT_LIBRARY_MARKER not in prompt
        assert "user_prompt:" not in prompt

    def test_headless_prompt_omits_all_three_at_once(self, headless):
        """The aggregate, kept only as the summary of the three above."""
        prompt = _headless_prompt(headless)
        for marker in (PROJECT_FILE_MARKER, PERSONAL_CONTEXT_MARKER, PROMPT_LIBRARY_MARKER):
            assert marker not in prompt

    def test_the_files_the_exclusion_is_about_all_exist_and_are_non_empty(self, operator_machine):
        """Guards the fixture itself: an empty file proves nothing was excluded."""
        import os

        state = os.environ["AXI_STATE_DIR"]
        present = [
            operator_machine / "CLAUDE.md",
            operator_machine / ".claude" / "context.md",
            operator_machine.parent / "operator-state" / "prompts" / "house_style.md",
        ]
        assert str(present[2]).startswith(state)
        for path in present:
            assert path.is_file(), path
            assert path.read_text(encoding="utf-8").strip(), path


# ---------------------------------------------------------------------------
# The terminal is untouched
# ---------------------------------------------------------------------------


class TestTerminalKeepsAllThree:
    """An operator at a keyboard sees exactly what it saw before the switch."""

    def test_terminal_prompt_contains_the_project_file(self, terminal_agent):
        prompt = terminal_agent._build_system_prompt()
        assert PROJECT_FILE_MARKER in prompt
        assert PROJECT_FILE_HEADER in prompt

    def test_terminal_prompt_contains_the_personal_context_file(self, terminal_agent):
        prompt = terminal_agent._build_system_prompt()
        assert PERSONAL_CONTEXT_MARKER in prompt
        assert PERSONAL_CONTEXT_HEADER in prompt

    def test_terminal_prompt_contains_the_operator_prompt_library(self, terminal_agent):
        prompt = terminal_agent._build_system_prompt()
        assert PROMPT_LIBRARY_MARKER in prompt

    def test_an_explicit_default_scope_composes_the_identical_prompt(self, terminal_agent):
        """Declaring the default changes nothing: same characters, both ways."""
        implicit = terminal_agent._build_system_prompt()
        explicit = terminal_agent._build_system_prompt(
            scope=ChatScope(
                session=terminal_agent.session,
                include_operator_local_prompts=True,
            )
        )
        assert implicit == explicit

    def test_a_terminal_turn_sends_all_three_to_the_model(self, terminal_agent, mock_gateway):
        terminal_agent.turn("hello", stream=False)
        system = _systems_sent(mock_gateway)[0]
        assert PROJECT_FILE_MARKER in system
        assert PERSONAL_CONTEXT_MARKER in system
        assert PROMPT_LIBRARY_MARKER in system


# ---------------------------------------------------------------------------
# Not first-request-only
# ---------------------------------------------------------------------------


class TestTheExclusionHoldsOnEveryRequest:
    """A leak that reopens on request two is still a leak."""

    def test_two_requests_on_one_headless_handle_both_exclude_all_three(
        self, headless, mock_gateway
    ):
        headless.turn("first question", stream=False)
        headless.turn("second question", stream=False)

        systems = _systems_sent(mock_gateway)
        assert len(systems) == 2
        for index, system in enumerate(systems):
            assert PROJECT_FILE_MARKER not in system, f"project file leaked on request {index}"
            assert PERSONAL_CONTEXT_MARKER not in system, f"personal context on request {index}"
            assert PROMPT_LIBRARY_MARKER not in system, f"prompt library on request {index}"

    def test_a_reused_scope_still_excludes_them_on_its_second_turn(self, headless, mock_gateway):
        """Continuing a conversation is the other way a second request arrives."""
        scope = headless.new_scope()
        headless.turn("first", stream=False, scope=scope)
        headless.turn("second", stream=False, scope=scope)

        systems = _systems_sent(mock_gateway)
        assert len(systems) == 2
        assert PROJECT_FILE_MARKER not in systems[1]
        assert PERSONAL_CONTEXT_MARKER not in systems[1]
        assert PROMPT_LIBRARY_MARKER not in systems[1]
        assert scope.include_operator_local_prompts is False

    def test_composing_a_prompt_does_not_change_the_scope_declaration(self, headless):
        """Nothing writes the flag back, so turn N cannot widen turn N+1."""
        scope = headless.new_scope()
        headless.agent._build_system_prompt(scope=scope)
        headless.agent._build_system_prompt(scope=scope)
        assert scope.include_operator_local_prompts is False


# ---------------------------------------------------------------------------
# The switch is declared, never inferred
# ---------------------------------------------------------------------------


class TestTheSwitchIsDeclared:
    """A surface says whether it wants them. The platform never guesses."""

    def test_the_scope_field_defaults_to_including_them(self):
        assert ChatScope().include_operator_local_prompts is True

    def test_a_headless_scope_declares_the_exclusion(self, headless):
        assert headless.new_scope().include_operator_local_prompts is False

    def test_the_default_scope_of_a_terminal_agent_declares_inclusion(self, terminal_agent):
        assert terminal_agent.scope.include_operator_local_prompts is True

    def test_one_environment_yields_both_answers(self, terminal_agent, headless):
        """Same process, same files, same env: only the declaration differs."""
        included = terminal_agent._build_system_prompt()
        excluded = _headless_prompt(headless)
        assert PROJECT_FILE_MARKER in included and PROJECT_FILE_MARKER not in excluded
        assert PERSONAL_CONTEXT_MARKER in included and PERSONAL_CONTEXT_MARKER not in excluded
        assert PROMPT_LIBRARY_MARKER in included and PROMPT_LIBRARY_MARKER not in excluded

    def test_neither_answer_consults_a_terminal(self, terminal_agent, headless, monkeypatch):
        """``isatty`` is not the signal: reading it at all fails these calls."""

        class _ExplodingStdin:
            def isatty(self) -> bool:
                raise AssertionError("prompt composition sniffed for a terminal")

            def read(self, *_a, **_kw):
                raise AssertionError("prompt composition read stdin")

        monkeypatch.setattr("sys.stdin", _ExplodingStdin())
        assert PROJECT_FILE_MARKER in terminal_agent._build_system_prompt()
        assert PROJECT_FILE_MARKER not in _headless_prompt(headless)

    def test_each_source_is_guarded_separately(self):
        """Three checks, one per source, so dropping one is visible in review.

        Read from the parsed code, not the text, so a comment or a docstring
        that mentions the field cannot stand in for a guard that runs.
        """
        guards = [
            node
            for node in ast.walk(_prompt_builder_ast())
            if isinstance(node, ast.If)
            and any(
                isinstance(sub, ast.Attribute) and sub.attr == "include_operator_local_prompts"
                for sub in ast.walk(node.test)
            )
        ]
        assert len(guards) == 3

    def test_the_guard_reads_the_scope_and_nothing_about_the_process(self):
        """The only thing consulted is the scope: no environment, no terminal."""
        names = {
            node.attr for node in ast.walk(_prompt_builder_ast()) if isinstance(node, ast.Attribute)
        } | {node.id for node in ast.walk(_prompt_builder_ast()) if isinstance(node, ast.Name)}
        for sniff in ("isatty", "environ", "getenv", "environb", "stdout", "stdin"):
            assert sniff not in names, f"{sniff!r} decides prompt content from the process"


# ---------------------------------------------------------------------------
# A hand-built scope cannot reintroduce it
# ---------------------------------------------------------------------------


class TestHandBuiltScopes:
    """The same guard shape the turn deadline already has."""

    def test_a_headless_turn_refuses_a_scope_that_includes_them(self, headless):
        scope = ChatScope(turn_deadline=30.0, include_operator_local_prompts=True)
        with pytest.raises(ValueError, match="operator"):
            headless.turn("question", stream=False, scope=scope)

    def test_the_refusal_names_the_way_to_build_the_scope(self, headless):
        scope = ChatScope(turn_deadline=30.0, include_operator_local_prompts=True)
        with pytest.raises(ValueError, match="new_scope"):
            headless.turn("question", stream=False, scope=scope)

    def test_a_scope_from_new_scope_is_accepted(self, headless, mock_gateway):
        headless.turn("question", stream=False, scope=headless.new_scope())
        assert PROJECT_FILE_MARKER not in _systems_sent(mock_gateway)[0]

    def test_a_bare_agent_with_an_excluding_scope_excludes(self, terminal_agent, mock_gateway):
        """The field works on any agent, not only through the headless handle."""
        terminal_agent.turn(
            "question",
            stream=False,
            scope=ChatScope(include_operator_local_prompts=False),
        )
        system = _systems_sent(mock_gateway)[0]
        assert PROJECT_FILE_MARKER not in system
        assert PERSONAL_CONTEXT_MARKER not in system
        assert PROMPT_LIBRARY_MARKER not in system

    def test_one_agent_answers_two_requests_differently(self, terminal_agent, mock_gateway):
        """The reason the switch is on the scope: per request, not per process."""
        terminal_agent.turn(
            "for somebody else",
            stream=False,
            scope=ChatScope(include_operator_local_prompts=False),
        )
        terminal_agent.turn("for the operator", stream=False)

        excluded, included = _systems_sent(mock_gateway)
        assert PROJECT_FILE_MARKER not in excluded
        assert PERSONAL_CONTEXT_MARKER not in excluded
        assert PROMPT_LIBRARY_MARKER not in excluded
        assert PROJECT_FILE_MARKER in included
        assert PERSONAL_CONTEXT_MARKER in included
        assert PROMPT_LIBRARY_MARKER in included
