# Copyright (c) 2026 B-Tree Ventures, LLC
# SPDX-License-Identifier: Apache-2.0

"""Tests for user-authored system-prompt fragments.

Closes the parity-doc gap 'Prompt libraries (reusable, user-editable
templates exposed)'. PromptComposer is internal infrastructure with
seven canonical layers (identity / capabilities / policies /
domain_context / session_memory / retrieved / live). Until now, only
extension code could add contributions. This loader gives users a
file-based surface — drop a .md into ~/.axi/prompts/ or the project's
.axi/prompts/ and it becomes a system-prompt contribution.

Mirrors the slash-commands pattern: project scope wins on collision.
"""

from __future__ import annotations


import pytest


@pytest.fixture
def prompts(tmp_path, monkeypatch):
    user_state = tmp_path / "user_state"
    user_state.mkdir()
    project = tmp_path / "project"
    project.mkdir()
    (project / ".git").mkdir()

    user_dir = user_state / "prompts"
    user_dir.mkdir()
    project_dir = project / ".axi" / "prompts"
    project_dir.mkdir(parents=True)

    monkeypatch.setenv("AXI_STATE_DIR", str(user_state))
    monkeypatch.setenv("AXIOM_ROOT", str(project))

    return user_dir, project_dir


def test_loader_returns_empty_when_no_prompts(prompts):
    from axiom.extensions.builtins.chat.user_prompts import load_user_prompts

    assert load_user_prompts() == []


def test_loader_finds_user_prompt(prompts):
    user_dir, _ = prompts
    (user_dir / "house_style.md").write_text("Always use Mermaid for diagrams.")

    from axiom.extensions.builtins.chat.user_prompts import load_user_prompts

    loaded = load_user_prompts()
    assert len(loaded) == 1
    assert loaded[0].name == "house_style"
    assert loaded[0].scope == "user"
    assert "Mermaid" in loaded[0].body


def test_loader_finds_project_prompt(prompts):
    _, project_dir = prompts
    (project_dir / "team_rules.md").write_text("Prefer pytest fixtures over setUp.")

    from axiom.extensions.builtins.chat.user_prompts import load_user_prompts

    loaded = load_user_prompts()
    assert len(loaded) == 1
    assert loaded[0].scope == "project"


def test_project_overrides_user_on_collision(prompts):
    user_dir, project_dir = prompts
    (user_dir / "rules.md").write_text("USER VERSION")
    (project_dir / "rules.md").write_text("PROJECT VERSION")

    from axiom.extensions.builtins.chat.user_prompts import load_user_prompts

    loaded = {p.name: p for p in load_user_prompts()}
    assert loaded["rules"].scope == "project"
    assert "PROJECT VERSION" in loaded["rules"].body


def test_default_layer_is_domain_context(prompts):
    user_dir, _ = prompts
    (user_dir / "default_layer.md").write_text("Plain body, no frontmatter.")

    from axiom.extensions.builtins.chat.user_prompts import load_user_prompts

    p = load_user_prompts()[0]
    assert p.layer == "domain_context"


def test_frontmatter_layer_override(prompts):
    user_dir, _ = prompts
    (user_dir / "policy.md").write_text(
        "---\nlayer: policies\ndescription: Strict citation rule\n---\n"
        "Always cite sources."
    )

    from axiom.extensions.builtins.chat.user_prompts import load_user_prompts

    p = load_user_prompts()[0]
    assert p.layer == "policies"
    assert p.description == "Strict citation rule"
    assert "---" not in p.body
    assert p.body.strip() == "Always cite sources."


def test_invalid_layer_falls_back_to_domain_context(prompts):
    """Frontmatter with a bogus layer name doesn't drop the prompt —
    it falls back to domain_context with a warning (silent in test)."""
    user_dir, _ = prompts
    (user_dir / "broken.md").write_text(
        "---\nlayer: not_a_real_layer\n---\nBody still works."
    )

    from axiom.extensions.builtins.chat.user_prompts import load_user_prompts

    p = load_user_prompts()[0]
    assert p.layer == "domain_context"
    assert "Body still works" in p.body


def test_skips_non_md_files(prompts):
    user_dir, _ = prompts
    (user_dir / "ignored.txt").write_text("not a prompt")
    (user_dir / "valid.md").write_text("valid prompt")

    from axiom.extensions.builtins.chat.user_prompts import load_user_prompts

    names = [p.name for p in load_user_prompts()]
    assert "valid" in names
    assert "ignored" not in names


def test_add_to_composer_inserts_contributions(prompts):
    user_dir, project_dir = prompts
    (user_dir / "house.md").write_text("House style: Mermaid only.")
    (project_dir / "team.md").write_text(
        "---\nlayer: policies\n---\nTeam policy: TDD always."
    )

    from axiom.extensions.builtins.chat.user_prompts import add_user_prompts_to
    from axiom.infra.prompt_composer import PromptComposer

    composer = PromptComposer()
    n = add_user_prompts_to(composer)
    assert n == 2

    rendered = composer.render_text()
    assert "Mermaid only" in rendered
    assert "TDD always" in rendered


def test_chat_agent_includes_user_prompts_in_system_prompt(prompts, tmp_path):
    """End-to-end: a user prompt under ~/.axi/prompts/ shows up in the
    chat agent's built system prompt."""
    user_dir, _ = prompts
    (user_dir / "voice.md").write_text("Be concise; no marketing tone.")

    from axiom.extensions.builtins.chat.agent import ChatAgent

    agent = ChatAgent()
    system = agent._build_system_prompt()
    assert "Be concise" in system


def test_loader_handles_missing_dirs_gracefully(tmp_path, monkeypatch):
    state = tmp_path / "state_empty"
    state.mkdir()
    monkeypatch.setenv("AXI_STATE_DIR", str(state))
    monkeypatch.setenv("AXIOM_ROOT", str(tmp_path / "no_project"))

    from axiom.extensions.builtins.chat.user_prompts import load_user_prompts

    assert load_user_prompts() == []
