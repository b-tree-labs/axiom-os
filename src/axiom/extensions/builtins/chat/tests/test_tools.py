# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tests for chat tool registry and execution."""

from axiom.extensions.builtins.chat.tools import (
    TOOL_REGISTRY,
    execute_tool,
    get_tool_definitions,
)
from axiom.infra.orchestrator.actions import ActionCategory


class TestToolRegistry:
    """Test tool definitions."""

    def test_read_tools_marked_read(self):
        reads = [t for t in TOOL_REGISTRY.values() if t.category == ActionCategory.READ]
        assert len(reads) >= 4
        names = {t.name for t in reads}
        assert "query_docs" in names
        assert "signal_status" in names
        assert "list_providers" in names

    def test_write_tools_marked_write(self):
        writes = [t for t in TOOL_REGISTRY.values() if t.category == ActionCategory.WRITE]
        assert len(writes) >= 3
        names = {t.name for t in writes}
        assert "doc_publish" in names
        assert "doc_generate" in names
        assert "write_inbox_note" in names

    def test_tool_definitions_format(self):
        defs = get_tool_definitions()
        assert isinstance(defs, list)
        assert len(defs) > 0
        for d in defs:
            assert d["type"] == "function"
            assert "function" in d
            assert "name" in d["function"]
            assert "description" in d["function"]


class TestToolExecution:
    """Test tool handlers."""

    def test_list_providers(self):
        result = execute_tool("list_providers", {})
        assert "generation" in result
        assert "storage" in result

    def test_query_docs_empty(self):
        result = execute_tool("query_docs", {})
        assert "documents" in result

    def test_signal_status(self):
        result = execute_tool("signal_status", {})
        assert "processed" in result
        assert "drafts" in result

    def test_write_inbox_note(self, tmp_path, monkeypatch):
        """Write a note to a temp inbox."""
        # Patch INBOX_RAW at the source module (deferred import in execute_tool)
        monkeypatch.setattr(
            "axiom.extensions.builtins.signals.cli.INBOX_RAW",
            tmp_path,
        )
        result = execute_tool("write_inbox_note", {"text": "Test note content"})
        assert "Note saved" in result["message"]
        notes = list(tmp_path.glob("note_*.md"))
        assert len(notes) == 1
        assert "Test note content" in notes[0].read_text()

    def test_unknown_tool(self):
        result = execute_tool("nonexistent_tool", {})
        assert "error" in result
