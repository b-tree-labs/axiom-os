# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tests for extension discovery and loading."""


import pytest

from axiom.extensions.discovery import (
    discover_all_skills,
    discover_and_load_chat_tools,
    discover_cli_commands,
    discover_extensions,
    execute_extension_tool,
    generate_contract_docs,
    load_chat_tools,
)
from axiom.extensions.scaffold import scaffold_extension


@pytest.fixture
def ext_base(tmp_path):
    """Create a base directory for extensions."""
    d = tmp_path / "extensions"
    d.mkdir()
    return d


@pytest.fixture
def scaffolded_ext(ext_base):
    """Create a scaffolded extension."""
    scaffold_extension("test-tools", base_dir=ext_base, author="Test")
    return ext_base


class TestDiscoverExtensions:
    def test_empty_dir(self, ext_base):
        exts = discover_extensions(ext_base)
        assert exts == []

    def test_discover_scaffolded(self, scaffolded_ext):
        exts = discover_extensions(scaffolded_ext)
        assert len(exts) == 1
        assert exts[0].name == "test-tools"

    def test_skip_invalid_dir(self, ext_base):
        (ext_base / "not-an-extension").mkdir()
        exts = discover_extensions(ext_base)
        assert exts == []

    def test_skip_files(self, ext_base):
        (ext_base / "random.txt").write_text("hello")
        exts = discover_extensions(ext_base)
        assert exts == []

    def test_multiple_extensions(self, ext_base):
        scaffold_extension("ext-alpha", base_dir=ext_base)
        scaffold_extension("ext-beta", base_dir=ext_base)
        exts = discover_extensions(ext_base)
        assert len(exts) == 2
        names = {e.name for e in exts}
        assert names == {"ext-alpha", "ext-beta"}

    def test_dedup_by_name(self, tmp_path):
        """First directory wins when same name appears in multiple dirs."""
        dir1 = tmp_path / "first"
        dir2 = tmp_path / "second"
        scaffold_extension("same-name", base_dir=dir1)
        scaffold_extension("same-name", base_dir=dir2)
        exts = discover_extensions(dir1, dir2)
        assert len(exts) == 1
        assert str(dir1) in str(exts[0].root)

    def test_nonexistent_dir(self, tmp_path):
        exts = discover_extensions(tmp_path / "nonexistent")
        assert exts == []

    def test_malformed_manifest_skipped(self, ext_base):
        bad = ext_base / "bad-ext"
        bad.mkdir()
        (bad / "axiom-extension.toml").write_text("this is not valid toml [[[")
        exts = discover_extensions(ext_base)
        assert exts == []


class TestLoadChatTools:
    def test_load_from_scaffold(self, scaffolded_ext):
        exts = discover_extensions(scaffolded_ext)
        tools = load_chat_tools(exts[0])
        assert len(tools) >= 1
        names = [t.name for t in tools]
        assert "data_query" in names

    def test_no_tools_module(self, ext_base):
        d = ext_base / "empty-ext"
        d.mkdir()
        (d / "axiom-extension.toml").write_text(
            '[extension]\nname = "empty-ext"\n'
        )
        exts = discover_extensions(ext_base)
        tools = load_chat_tools(exts[0])
        assert tools == []


class TestDiscoverAndLoadChatTools:
    def test_full_discovery(self, scaffolded_ext):
        tools = discover_and_load_chat_tools(scaffolded_ext)
        assert len(tools) >= 1
        assert any(t.name == "data_query" for t in tools)

    def test_empty(self, ext_base):
        tools = discover_and_load_chat_tools(ext_base)
        assert tools == []


class TestExecuteExtensionTool:
    def test_execute_scaffolded_tool(self, scaffolded_ext):
        result = execute_extension_tool(
            "data_query", {"query": "power"}, scaffolded_ext
        )
        assert result is not None
        assert "results" in result

    def test_unknown_tool_returns_none(self, scaffolded_ext):
        result = execute_extension_tool(
            "nonexistent_tool", {}, scaffolded_ext
        )
        assert result is None

    def test_no_extensions_returns_none(self, ext_base):
        result = execute_extension_tool("anything", {}, ext_base)
        assert result is None


class TestDiscoverCLICommands:
    def test_discover_from_scaffold(self, scaffolded_ext):
        cmds = discover_cli_commands(scaffolded_ext)
        assert "logs" in cmds
        assert cmds["logs"]["extension"] == "test-tools"

    def test_empty(self, ext_base):
        cmds = discover_cli_commands(ext_base)
        assert cmds == {}


class TestDiscoverAllSkills:
    def test_discover_from_scaffold(self, scaffolded_ext):
        skills = discover_all_skills(scaffolded_ext)
        assert len(skills) >= 1
        assert any(s.name == "weekly-slides" for s in skills)

    def test_empty(self, ext_base):
        skills = discover_all_skills(ext_base)
        assert skills == []


class TestGenerateContractDocs:
    def test_generates_markdown(self):
        docs = generate_contract_docs()
        assert "# Extension Contracts" in docs
        assert "axiom-extension.toml" in docs
        assert "ToolDef" in docs
        assert "SKILL.md" in docs
        assert "GenerationProvider" in docs
