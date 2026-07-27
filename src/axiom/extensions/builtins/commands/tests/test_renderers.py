# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tests for the per-harness renderers."""

from __future__ import annotations

import json

from axiom.extensions.builtins.commands.discovery import (
    CliNoun,
    CommandTree,
    SlashCommand,
    Verb,
)
from axiom.extensions.builtins.commands.renderers import (
    claude,
    codex,
    cursor,
    neovim,
    opencode,
    vim,
    vscode,
)


def _sample_tree() -> CommandTree:
    tree = CommandTree()
    tree.nouns["tidy"] = CliNoun(
        noun="tidy",
        extension="hygiene",
        description="TIDY resource steward",
        module="axiom.extensions.builtins.hygiene.cli",
        function="main",
        tier="builtin",
        verbs=(
            Verb(name="status", help="Show status"),
            Verb(name="worktrees", help="Stale-worktree skill", args=("repo",)),
        ),
    )
    tree.slash_commands["help"] = SlashCommand(
        name="help", extension="chat", description="Show help"
    )
    return tree


# ---- Claude --------------------------------------------------------------


def test_claude_emits_nested_md(tmp_path):
    files = claude.render(_sample_tree(), tmp_path)
    paths = {f.relative_to(tmp_path).as_posix() for f in files}
    assert ".claude/commands/axi/tidy/status.md" in paths
    assert ".claude/commands/axi/tidy/worktrees.md" in paths
    assert ".claude/commands/axi/chat/help.md" in paths


def test_claude_shim_has_axi_invocation(tmp_path):
    claude.render(_sample_tree(), tmp_path)
    body = (tmp_path / ".claude/commands/axi/tidy/status.md").read_text()
    assert "axi tidy status" in body
    assert "allowed-tools" in body


def test_claude_shim_records_argument_hint(tmp_path):
    claude.render(_sample_tree(), tmp_path)
    body = (tmp_path / ".claude/commands/axi/tidy/worktrees.md").read_text()
    assert "argument-hint" in body
    assert "<repo>" in body


# ---- Cursor --------------------------------------------------------------


def test_cursor_emits_mcp_json_and_flat_commands(tmp_path):
    cursor.render(_sample_tree(), tmp_path)
    mcp = json.loads((tmp_path / ".cursor/mcp.json").read_text())
    assert "axiom" in mcp["mcpServers"]
    assert (tmp_path / ".cursor/commands/axi-tidy-status.md").exists()
    assert (tmp_path / ".cursor/commands/axi-tidy-worktrees.md").exists()


# ---- Codex ---------------------------------------------------------------


def test_codex_inserts_idempotent_block(tmp_path):
    files1 = codex.render(_sample_tree(), tmp_path)
    files2 = codex.render(_sample_tree(), tmp_path)
    assert files1 == files2
    body = (tmp_path / ".codex/config.toml").read_text()
    # Block should appear exactly once
    assert body.count("[mcp_servers.axiom]") == 1
    assert body.count(">>> axi commands generate") == 1


def test_codex_preserves_other_keys(tmp_path):
    config = tmp_path / ".codex" / "config.toml"
    config.parent.mkdir(parents=True)
    config.write_text("[other]\nfoo = 1\n", encoding="utf-8")
    codex.render(_sample_tree(), tmp_path)
    body = config.read_text()
    assert "[other]" in body
    assert "foo = 1" in body
    assert "[mcp_servers.axiom]" in body


# ---- VS Code -------------------------------------------------------------


def test_vscode_emits_mcp_and_tasks(tmp_path):
    vscode.render(_sample_tree(), tmp_path)
    mcp = json.loads((tmp_path / ".vscode/mcp.json").read_text())
    assert "axiom" in mcp["servers"]
    tasks = json.loads((tmp_path / ".vscode/tasks.json").read_text())
    labels = {t["label"] for t in tasks["tasks"]}
    assert "axi tidy status" in labels
    assert "axi tidy worktrees" in labels


def test_vscode_threads_inputs_for_args(tmp_path):
    vscode.render(_sample_tree(), tmp_path)
    tasks = json.loads((tmp_path / ".vscode/tasks.json").read_text())
    inputs = {i["id"] for i in tasks.get("inputs", [])}
    assert "repo" in inputs


# ---- OpenCode ------------------------------------------------------------


def test_opencode_registers_axiom_server(tmp_path):
    opencode.render(_sample_tree(), tmp_path)
    cfg = json.loads((tmp_path / ".opencode/opencode.json").read_text())
    assert cfg["mcp"]["servers"]["axiom"]["command"] == "axi"


def test_opencode_merges_with_existing(tmp_path):
    cfg_path = tmp_path / ".opencode/opencode.json"
    cfg_path.parent.mkdir(parents=True)
    cfg_path.write_text(
        json.dumps({"theme": "dark", "mcp": {"servers": {"existing": {}}}}),
        encoding="utf-8",
    )
    opencode.render(_sample_tree(), tmp_path)
    cfg = json.loads(cfg_path.read_text())
    assert cfg["theme"] == "dark"
    assert "existing" in cfg["mcp"]["servers"]
    assert "axiom" in cfg["mcp"]["servers"]


# ---- Neovim --------------------------------------------------------------


def test_neovim_emits_lua_with_user_command(tmp_path):
    neovim.render(_sample_tree(), tmp_path)
    plugin = (tmp_path / ".axi/shims/neovim/lua/axi.lua").read_text()
    assert 'nvim_create_user_command("Axi"' in plugin
    assert '"tidy"' in plugin
    assert '"status"' in plugin


# ---- Vim -----------------------------------------------------------------


def test_vim_emits_command_with_completion(tmp_path):
    vim.render(_sample_tree(), tmp_path)
    plugin = (tmp_path / ".axi/shims/vim/plugin/axi.vim").read_text()
    assert ":Axi" in plugin or "command! -nargs=+" in plugin
    assert "tidy" in plugin
    assert "status" in plugin
