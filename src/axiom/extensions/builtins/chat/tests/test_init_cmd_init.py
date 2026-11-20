# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""/init creates AGENTS.md when absent and otherwise touches only its own managed region, with --yes."""

from __future__ import annotations

import pytest

from axiom.extensions.builtins.chat.agent import ChatAgent
from axiom.extensions.builtins.chat.builtin_commands import ensure_loaded
from axiom.extensions.builtins.chat.command_registry import REGISTRY, CommandContext
from axiom.extensions.builtins.chat.commands_ext.init import BEGIN, END
from axiom.infra.orchestrator.session import Session
from axiom.memory.rendering import MANAGED_BLOCK_BEGIN, MANAGED_BLOCK_END


@pytest.fixture
def proj(tmp_path, monkeypatch):
    root = tmp_path / "proj"
    root.mkdir()
    (root / ".git").mkdir()
    (root / "pyproject.toml").write_text("[project]\nname='x'\n")
    (root / "src").mkdir()
    monkeypatch.setenv("AXIOM_ROOT", str(root))
    return root


def _run(line):
    ensure_loaded()
    return REGISTRY.dispatch(line, CommandContext(agent=ChatAgent(session=Session()), store=None))


def test_absent_file_is_created_from_the_template_with_facts_and_a_claude_link(proj):
    out = _run("/init")
    text = (proj / "AGENTS.md").read_text()
    assert "Created" in out
    assert BEGIN in text and END in text
    assert "Python project (pyproject.toml)" in text and "`src/`" in text
    assert "$PROJECT" not in text and "$BLOCK" not in text and "$CLI" not in text
    assert (proj / "CLAUDE.md").is_symlink()
    assert (proj / "CLAUDE.md").read_text() == text


def test_an_existing_regular_claude_md_is_not_replaced(proj):
    (proj / "CLAUDE.md").write_text("mine\n")
    _run("/init")
    assert not (proj / "CLAUDE.md").is_symlink()
    assert (proj / "CLAUDE.md").read_text() == "mine\n"


def test_existing_file_is_not_written_without_yes_and_a_diff_is_shown(proj):
    original = "# Mine\n\nHand written rules.\n"
    (proj / "AGENTS.md").write_text(original)
    out = _run("/init")
    assert (proj / "AGENTS.md").read_text() == original
    assert "+" + BEGIN in out and "--yes" in out and "Nothing written" in out


def test_yes_writes_only_the_managed_region_and_keeps_everything_else(proj):
    (proj / "AGENTS.md").write_text(
        f"# Mine\n\nrule one\n\n{MANAGED_BLOCK_BEGIN}\nmemory stuff\n{MANAGED_BLOCK_END}\n\ntail rules\n"
    )
    _run("/init --yes")
    first = (proj / "AGENTS.md").read_text()
    assert first.startswith("# Mine\n\nrule one\n")
    assert f"{MANAGED_BLOCK_BEGIN}\nmemory stuff\n{MANAGED_BLOCK_END}" in first  # cross-mem block untouched
    assert "tail rules" in first and BEGIN in first
    # change the tree, run again: only the init region moves
    (proj / "go.mod").write_text("module x\n")
    _run("/init --yes")
    second = (proj / "AGENTS.md").read_text()
    assert "Go module (go.mod)" in second
    assert second.replace(second[second.index(BEGIN):second.index(END)], "") == first.replace(
        first[first.index(BEGIN):first.index(END)], "")


def test_second_run_is_a_no_op(proj):
    _run("/init")
    before = (proj / "AGENTS.md").read_text()
    out = _run("/init --yes")
    assert "up to date" in out and (proj / "AGENTS.md").read_text() == before


def test_unbalanced_markers_are_refused_untouched(proj):
    bad = f"# x\n{BEGIN}\nno end\n"
    (proj / "AGENTS.md").write_text(bad)
    out = _run("/init --yes")
    assert "unbalanced" in out and (proj / "AGENTS.md").read_text() == bad


def test_symlinked_instruction_file_is_written_through_not_replaced(proj):
    (proj / "AGENTS.md").write_text("# real\n")
    (proj / "CLAUDE.md").symlink_to("AGENTS.md")
    _run("/init --yes")
    assert (proj / "CLAUDE.md").is_symlink()
    assert BEGIN in (proj / "AGENTS.md").read_text()


def test_unknown_option_is_refused(proj):
    assert "unknown option" in _run("/init --force")
    assert not (proj / "AGENTS.md").exists()
