# Copyright (c) 2026 B-Tree Ventures, LLC
# SPDX-License-Identifier: Apache-2.0

"""Tests for the user-defined slash command loader.

User-defined slash commands mirror Claude Code's ``.claude/commands/*.md``
pattern: each ``.md`` file becomes a ``/<name>`` command whose body is a
prompt template sent to the chat agent. Optional YAML frontmatter carries
``description`` and ``argument-hint``. ``$ARGUMENTS`` in the body is
substituted with whatever the user typed after the command name.

Resolution order on name collision: project scope wins over user scope.
"""

from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture
def cmds(tmp_path: Path, monkeypatch):
    """Isolated user + project command dirs."""
    user_state = tmp_path / "user_state"
    user_state.mkdir()
    project = tmp_path / "project"
    project.mkdir()
    (project / ".git").mkdir()  # mark as project root for get_project_root()

    user_dir = user_state / "commands"
    user_dir.mkdir()
    project_dir = project / ".axi" / "commands"
    project_dir.mkdir(parents=True)

    monkeypatch.setenv("AXI_STATE_DIR", str(user_state))
    monkeypatch.setenv("AXIOM_ROOT", str(project))

    return user_dir, project_dir


def test_loader_returns_empty_when_no_commands(cmds):
    from axiom.extensions.builtins.chat.user_commands import load_user_commands

    assert load_user_commands() == {}


def test_loader_finds_user_command(cmds):
    user_dir, _ = cmds
    (user_dir / "explain.md").write_text("Explain this concept simply: $ARGUMENTS")

    from axiom.extensions.builtins.chat.user_commands import load_user_commands

    cmds_map = load_user_commands()
    assert "explain" in cmds_map
    assert cmds_map["explain"].scope == "user"
    assert "$ARGUMENTS" in cmds_map["explain"].body


def test_loader_finds_project_command(cmds):
    _, project_dir = cmds
    (project_dir / "review.md").write_text("Review this PR: $ARGUMENTS")

    from axiom.extensions.builtins.chat.user_commands import load_user_commands

    cmds_map = load_user_commands()
    assert "review" in cmds_map
    assert cmds_map["review"].scope == "project"


def test_project_overrides_user_on_collision(cmds):
    user_dir, project_dir = cmds
    (user_dir / "summarize.md").write_text("USER VERSION: $ARGUMENTS")
    (project_dir / "summarize.md").write_text("PROJECT VERSION: $ARGUMENTS")

    from axiom.extensions.builtins.chat.user_commands import load_user_commands

    cmds_map = load_user_commands()
    assert cmds_map["summarize"].scope == "project"
    assert "PROJECT VERSION" in cmds_map["summarize"].body


def test_frontmatter_description_extracted(cmds):
    user_dir, _ = cmds
    (user_dir / "quiz.md").write_text(
        "---\n"
        "description: Quiz me on a topic\n"
        "argument-hint: <topic>\n"
        "---\n"
        "Generate three quiz questions about: $ARGUMENTS"
    )

    from axiom.extensions.builtins.chat.user_commands import load_user_commands

    cmd = load_user_commands()["quiz"]
    assert cmd.description == "Quiz me on a topic"
    assert cmd.argument_hint == "<topic>"
    # Body must NOT include the frontmatter
    assert "---" not in cmd.body
    assert "description:" not in cmd.body
    assert "Generate three quiz questions" in cmd.body


def test_no_frontmatter_uses_filename_as_description(cmds):
    user_dir, _ = cmds
    (user_dir / "explain.md").write_text("Plain body, no frontmatter.")

    from axiom.extensions.builtins.chat.user_commands import load_user_commands

    cmd = load_user_commands()["explain"]
    assert cmd.description  # something non-empty
    assert cmd.body == "Plain body, no frontmatter."


def test_render_substitutes_arguments(cmds):
    user_dir, _ = cmds
    (user_dir / "explain.md").write_text("Explain: $ARGUMENTS")

    from axiom.extensions.builtins.chat.user_commands import load_user_commands, render_command

    cmd = load_user_commands()["explain"]
    assert render_command(cmd, "quantum tunneling") == "Explain: quantum tunneling"


def test_render_with_no_arguments_substitutes_empty(cmds):
    user_dir, _ = cmds
    (user_dir / "explain.md").write_text("Explain: $ARGUMENTS")

    from axiom.extensions.builtins.chat.user_commands import load_user_commands, render_command

    cmd = load_user_commands()["explain"]
    assert render_command(cmd, "") == "Explain: "


def test_render_keeps_body_unchanged_when_no_placeholder(cmds):
    user_dir, _ = cmds
    (user_dir / "joke.md").write_text("Tell me a joke.")

    from axiom.extensions.builtins.chat.user_commands import load_user_commands, render_command

    cmd = load_user_commands()["joke"]
    assert render_command(cmd, "anything") == "Tell me a joke."


def test_invalid_frontmatter_loads_body_anyway(cmds):
    user_dir, _ = cmds
    (user_dir / "broken.md").write_text(
        "---\n"
        "this is not: valid: yaml: at: all\n"
        "---\n"
        "Body still works."
    )

    from axiom.extensions.builtins.chat.user_commands import load_user_commands

    cmd = load_user_commands().get("broken")
    assert cmd is not None
    assert "Body still works." in cmd.body


def test_loader_handles_missing_dirs_gracefully(tmp_path, monkeypatch):
    """No user dir, no project dir, no .axi/commands — must return {}."""
    state = tmp_path / "state_empty"
    state.mkdir()
    monkeypatch.setenv("AXI_STATE_DIR", str(state))
    monkeypatch.setenv("AXIOM_ROOT", str(tmp_path / "no_project"))

    from axiom.extensions.builtins.chat.user_commands import load_user_commands

    assert load_user_commands() == {}


def test_skips_non_md_files(cmds):
    user_dir, _ = cmds
    (user_dir / "ignored.txt").write_text("not a command")
    (user_dir / "valid.md").write_text("Valid command.")

    from axiom.extensions.builtins.chat.user_commands import load_user_commands

    cmds_map = load_user_commands()
    assert "valid" in cmds_map
    assert "ignored" not in cmds_map


def test_get_slash_commands_includes_user_commands(cmds):
    """User commands surface in the help/tab-complete index."""
    user_dir, _ = cmds
    (user_dir / "practice.md").write_text(
        "---\ndescription: Practice problem set\n---\nGive me 3 problems on $ARGUMENTS"
    )

    from axiom.extensions.builtins.chat.commands import get_slash_commands

    all_cmds = get_slash_commands()
    assert "/practice" in all_cmds
    assert "Practice problem set" in all_cmds["/practice"]


def test_dispatcher_returns_prompt_for_user_command(cmds, monkeypatch):
    """Typing /practice 'fission cross-sections' returns the rendered prompt
    so the caller can route it to agent.turn() instead of printing it."""
    user_dir, _ = cmds
    (user_dir / "practice.md").write_text("Practice problems on: $ARGUMENTS")

    from axiom.extensions.builtins.chat.user_commands import (
        UserCommandPrompt,
        try_dispatch_user_command,
    )

    result = try_dispatch_user_command("/practice fission cross-sections")
    assert isinstance(result, UserCommandPrompt)
    assert result.prompt == "Practice problems on: fission cross-sections"
    assert result.command_name == "practice"


def test_dispatcher_returns_none_for_unknown_command(cmds):
    from axiom.extensions.builtins.chat.user_commands import try_dispatch_user_command

    assert try_dispatch_user_command("/totally-unknown") is None


def test_cli_handle_slash_returns_user_command_prompt(cmds, monkeypatch):
    """End-to-end: cli._handle_slash_command returns a UserCommandPrompt
    for a user-defined command instead of an 'unknown command' string."""
    user_dir, _ = cmds
    (user_dir / "explain.md").write_text("Explain this concept simply: $ARGUMENTS")

    from types import SimpleNamespace
    from unittest.mock import MagicMock

    from axiom.extensions.builtins.chat.cli import _handle_slash_command
    from axiom.extensions.builtins.chat.user_commands import UserCommandPrompt

    fake_agent = SimpleNamespace(session=SimpleNamespace(messages=[]), gateway=MagicMock())
    fake_store = MagicMock()

    result = _handle_slash_command("/explain entropy", fake_agent, fake_store)
    assert isinstance(result, UserCommandPrompt)
    assert result.command_name == "explain"
    assert result.prompt == "Explain this concept simply: entropy"


def test_unknown_slash_still_returns_string(cmds, monkeypatch):
    """Genuinely unknown commands still return the suggestion string,
    not a UserCommandPrompt."""
    from types import SimpleNamespace
    from unittest.mock import MagicMock

    from axiom.extensions.builtins.chat.cli import _handle_slash_command

    fake_agent = SimpleNamespace(session=SimpleNamespace(messages=[]), gateway=MagicMock())
    fake_store = MagicMock()

    result = _handle_slash_command("/totally-not-a-real-thing", fake_agent, fake_store)
    assert isinstance(result, str)
    assert "Unknown command" in result
