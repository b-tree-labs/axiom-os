# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""/review as a command: plan semantics for one turn, and a user template of the same name wins."""

from __future__ import annotations

import subprocess

import pytest

from axiom.extensions.builtins.chat.agent import ChatAgent
from axiom.extensions.builtins.chat.builtin_commands import ensure_loaded
from axiom.extensions.builtins.chat.command_registry import REGISTRY, CommandContext
from axiom.extensions.builtins.chat.user_commands import UserCommandPrompt
from axiom.infra.orchestrator.session import Session


def _git(root, *a):
    subprocess.run(["git", "-C", str(root), "-c", "user.name=t", "-c", "user.email=t@example.com", *a],
                   capture_output=True, text=True, check=True)


@pytest.fixture
def env(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    (root / "a.txt").write_text("one\n")
    _git(root, "add", ".")
    _git(root, "commit", "-qm", "base")
    state = tmp_path / "state"
    (state / "commands").mkdir(parents=True)
    monkeypatch.setenv("AXIOM_ROOT", str(root))
    monkeypatch.setenv("AXI_STATE_DIR", str(state))
    agent = ChatAgent(session=Session())
    agent.set_interaction_mode("agent")
    return root, state / "commands", agent


def _run(agent, line):
    ensure_loaded()
    return REGISTRY.dispatch(line, CommandContext(agent=agent, store=None))


def test_review_routes_a_prompt_with_the_diff_in_one_shot_plan_mode(env):
    root, _, agent = env
    (root / "a.txt").write_text("one\nSENTINEL_LINE\n")
    out = _run(agent, "/review")
    assert isinstance(out, UserCommandPrompt) and out.command_name == "review"
    assert "+SENTINEL_LINE" in out.prompt
    assert "$DIFF" not in out.prompt and "$ARGUMENTS" not in out.prompt
    assert agent.__dict__["_default_scope"].next_turn_mode == "plan"
    assert agent._interaction_mode == "agent"  # the standing mode is untouched


def test_review_with_nothing_to_review_says_so_and_requests_no_mode(env):
    _, _, agent = env
    out = _run(agent, "/review")
    assert isinstance(out, str) and "Nothing to review" in out
    assert agent.__dict__["_default_scope"].next_turn_mode == ""


def test_review_errors_are_messages_not_crashes(env):
    _, _, agent = env
    assert "no such branch" in _run(agent, "/review --branch nope")


def test_user_template_of_the_same_name_overrides_the_bundled_one(env):
    root, cmds, agent = env
    (root / "a.txt").write_text("one\nSENTINEL_LINE\n")
    (cmds / "review.md").write_text("MY OWN REVIEW of $ARGUMENTS\n$DIFF\n")
    out = _run(agent, "/review a.txt")
    assert out.prompt.startswith("MY OWN REVIEW of a.txt")
    assert "+SENTINEL_LINE" in out.prompt
    assert "senior reviewer" not in out.prompt  # the bundled text is gone
    # An override is the user's own: it does not force a mode.
    assert agent.__dict__["_default_scope"].next_turn_mode == ""


def test_override_without_a_diff_placeholder_still_receives_the_diff(env):
    root, cmds, agent = env
    (root / "a.txt").write_text("one\nSENTINEL_LINE\n")
    (cmds / "review.md").write_text("Just look.\n")
    assert "+SENTINEL_LINE" in _run(agent, "/review").prompt


def test_template_subcommand_points_at_the_bundled_file(env):
    _, _, agent = env
    out = _run(agent, "/review --template")
    assert "templates/review.md" in out and "$DIFF" in out


def test_bundled_template_uses_the_user_command_format():
    from axiom.extensions.builtins.chat.commands_ext._bundled import bundled

    cmd = bundled("review")
    assert cmd.description and "$ARGUMENTS" in cmd.body and "$DIFF" in cmd.body
