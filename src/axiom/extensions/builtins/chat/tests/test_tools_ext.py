# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tests for tool extension discovery and hot-reload."""

import sys

from axiom.extensions.builtins.chat.tools import (
    _ext_cache,
    _scan_extensions,
    get_all_tools,
)


class TestExtensionDiscovery:
    """Test that tools_ext/ modules are auto-discovered."""

    def test_builtin_tools_always_present(self):
        tools = get_all_tools()
        assert "query_docs" in tools
        assert "signal_status" in tools
        assert "list_providers" in tools

    def test_read_file_discovered(self):
        tools = get_all_tools()
        assert "read_file" in tools
        assert tools["read_file"].description
        assert tools["read_file"].parameters.get("properties", {}).get("path")

    def test_list_files_discovered(self):
        tools = get_all_tools()
        assert "list_files" in tools
        assert tools["list_files"].description

    def test_extension_tool_discovered(self, tmp_path, monkeypatch):
        """New tool file in tools_ext/ is auto-discovered."""
        pkg_dir = tmp_path / "tools_ext"
        pkg_dir.mkdir()
        (pkg_dir / "__init__.py").touch()
        (pkg_dir / "my_tool.py").write_text(
            "from axiom.extensions.builtins.chat.tools import ToolDef\n"
            "from axiom.infra.orchestrator.actions import ActionCategory\n"
            "\n"
            'TOOLS = [ToolDef(name="my_tool", description="Test tool", '
            'category=ActionCategory.READ, parameters={"type": "object", "properties": {}})]\n'
            "\n"
            "def execute(name, params):\n"
            '    return {"result": "ok"}\n'
        )
        monkeypatch.setattr("axiom.extensions.builtins.chat.tools._EXT_DIR", pkg_dir)
        _ext_cache.clear()

        # Clean up any stale module references
        mod_name = "axiom.extensions.builtins.chat.tools_ext.my_tool"
        sys.modules.pop(mod_name, None)

        try:
            tools = get_all_tools()
            assert "my_tool" in tools
        finally:
            sys.modules.pop(mod_name, None)

    def test_broken_extension_doesnt_crash(self, tmp_path, monkeypatch):
        """Malformed extension file is skipped gracefully."""
        ext_dir = tmp_path / "tools_ext"
        ext_dir.mkdir()
        (ext_dir / "__init__.py").touch()
        (ext_dir / "broken.py").write_text("raise ImportError('bad')")
        monkeypatch.setattr("axiom.extensions.builtins.chat.tools._EXT_DIR", ext_dir)
        _ext_cache.clear()

        tools = get_all_tools()
        assert "broken" not in tools
        # Built-in tools still work
        assert "query_docs" in tools

    def test_no_ext_dir(self, tmp_path, monkeypatch):
        """Missing tools_ext/ directory doesn't crash."""
        monkeypatch.setattr(
            "axiom.extensions.builtins.chat.tools._EXT_DIR", tmp_path / "nonexistent"
        )
        _ext_cache.clear()

        ext = _scan_extensions()
        assert ext == {}


class TestReadFileTool:
    """Test the read_file extension tool."""

    def test_read_existing_file(self):
        from axiom.extensions.builtins.chat.tools import execute_tool

        result = execute_tool("read_file", {"path": "pyproject.toml"})
        assert "content" in result
        assert "axiom" in result["content"]

    def test_read_nonexistent_file(self):
        from axiom.extensions.builtins.chat.tools import execute_tool

        result = execute_tool("read_file", {"path": "nonexistent_file_xyz.txt"})
        assert "error" in result

    def test_path_traversal_blocked(self):
        from axiom.extensions.builtins.chat.tools import execute_tool

        result = execute_tool("read_file", {"path": "../../../etc/passwd"})
        assert "error" in result
        assert "outside" in result["error"].lower()


class TestListFilesTool:
    """Test the list_files extension tool."""

    def test_list_root(self):
        from axiom.extensions.builtins.chat.tools import execute_tool

        result = execute_tool("list_files", {"path": "."})
        assert "files" in result
        assert "directories" in result

    def test_list_specific_dir(self):
        from axiom.extensions.builtins.chat.tools import execute_tool

        result = execute_tool("list_files", {"path": "src"})
        assert "files" in result or "directories" in result

    def test_list_nonexistent_dir(self):
        from axiom.extensions.builtins.chat.tools import execute_tool

        result = execute_tool("list_files", {"path": "nonexistent_dir_xyz"})
        assert "error" in result
