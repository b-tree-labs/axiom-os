# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""The 6-harness coverage matrix (cross-mem A2 scope item 4).

One readable table a person can scan to see, per harness, that each of the
three cross-mem capabilities is either **live** or its gap is **recorded**:

    absorb-read   — a live read-side adapter for the authored rules/instruction
                    layer (bidirectionality: we read the user's edits back).
    mcp-recall    — the axiom_memory_recall tool is exposed to the harness's
                    MCP client (deep stdio contract: test_mcp_client_conformance).
    write-back    — a managed instruction-file target Axiom writes.

Where a cell is genuinely not coverable — a harness whose auto-memory store the
survey does not path (Cursor Memories, Cline memory-bank, Continue→Mem0), or the
default-service access-graph gap that keeps stdio recall from serving — the
matrix asserts the open question is **recorded** in
`docs/working/cross-mem-a2-open-questions.md` rather than letting the cell pass
silently.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
OPEN_QUESTIONS = REPO_ROOT / "docs" / "working" / "cross-mem-a2-open-questions.md"

ACCOUNT = "acct-local"

# The readable matrix. Each row: the live capabilities + any recorded-gap token
# for an auto store the survey names but does not path.
#
#   harness    | absorb-read adapter        | write-back file            | auto-store gap
#   -----------+----------------------------+----------------------------+----------------
COVERAGE: dict[str, dict] = {
    "claude-code": {
        "absorb_read": ("markdown_hierarchy", "claude_code_adapter"),
        "mcp_recall": True,
        "write_back_product": "claude_code",
        "write_back_file": "CLAUDE.md",
        "auto_store_oq": None,  # cluster-1 reader already covers auto-memory dirs
    },
    "codex": {
        "absorb_read": ("structured_store", "codex_adapter"),
        "mcp_recall": True,
        "write_back_product": "agents_md",
        "write_back_file": "AGENTS.md",
        "auto_store_oq": None,  # cluster-2 Codex SQLite IS the auto-store reader
    },
    "cursor": {
        "absorb_read": ("rules_files", "cursor_adapter"),
        "mcp_recall": True,
        "write_back_product": "cursor",
        "write_back_file": ".cursor/rules/axiom-memory.md",
        "auto_store_oq": "OQ-A2-2",  # Cursor Memories — server-side, no read path
    },
    "cline": {
        "absorb_read": ("rules_files", "cline_adapter"),
        "mcp_recall": True,
        "write_back_product": "cline",
        "write_back_file": ".clinerules",
        "auto_store_oq": "OQ-A2-3",  # memory-bank — convention un-pathed by survey
    },
    "continue": {
        "absorb_read": ("rules_files", "continue_adapter"),
        "mcp_recall": True,
        "write_back_product": "continue",
        "write_back_file": ".continue/rules/axiom-memory.md",
        "auto_store_oq": "OQ-A2-4",  # Continue→Mem0 — third-party cloud vector
    },
    "roo": {
        "absorb_read": ("rules_files", "roo_adapter"),
        "mcp_recall": True,
        "write_back_product": "roo",
        "write_back_file": ".roo/rules/axiom-memory.md",
        "auto_store_oq": None,  # survey documents no separate auto store
    },
}

# The access-graph contradiction (Q1) applies to every harness's stdio recall.
GLOBAL_OQ = "OQ-A2-1"


def _seed_codex_home(home: Path) -> None:
    codex = home / ".codex"
    codex.mkdir(parents=True)
    con = sqlite3.connect(codex / "memories_1.sqlite")
    con.execute(
        "CREATE TABLE stage1_outputs (thread_id TEXT PRIMARY KEY, "
        "source_updated_at INTEGER NOT NULL, raw_memory TEXT NOT NULL, "
        "rollout_summary TEXT NOT NULL, generated_at INTEGER NOT NULL, "
        "usage_count INTEGER)"
    )
    con.execute(
        "INSERT INTO stage1_outputs VALUES ('t-1', 1752000000, "
        "'Prefers uv over pip.', 'pkg pref', 1752000001, 2)"
    )
    con.commit()
    con.close()


def _build_live_reader(harness: str, base: Path):
    """Construct the absorb-read adapter for ``harness`` over a seeded fixture."""
    if harness == "claude-code":
        from axiom.memory.absorb.markdown_hierarchy import claude_code_adapter

        (base / ".claude").mkdir(parents=True)
        (base / ".claude" / "CLAUDE.md").write_text("# Rules\n\nAnswer briefly.\n")
        return claude_code_adapter(account=ACCOUNT, home=base)
    if harness == "codex":
        from axiom.memory.absorb.structured_store import codex_adapter

        _seed_codex_home(base)
        return codex_adapter(account=ACCOUNT, home=base)

    from axiom.memory.absorb import rules_files

    factory = getattr(rules_files, COVERAGE[harness]["absorb_read"][1])
    layout = {
        "cursor": (".cursor/rules", "style.mdc"),
        "cline": (".clinerules", None),
        "continue": (".continue/rules", "py.md"),
        "roo": (".roo/rules", "conv.md"),
    }[harness]
    rel, filename = layout
    if filename is None:  # single-file rules (.clinerules)
        base.mkdir(parents=True, exist_ok=True)
        (base / rel).write_text(f"# {harness} rules\n\nUse spaces not tabs.\n")
    else:
        d = base / rel
        d.mkdir(parents=True)
        (d / filename).write_text(f"# {harness} rules\n\nUse spaces not tabs.\n")
    return factory(account=ACCOUNT, root=base)


@pytest.fixture(scope="module")
def open_questions_text() -> str:
    assert OPEN_QUESTIONS.is_file(), f"missing open-questions doc: {OPEN_QUESTIONS}"
    return OPEN_QUESTIONS.read_text(encoding="utf-8")


class TestCoverageMatrix:
    def test_matrix_covers_all_six_mcp_harnesses(self):
        assert set(COVERAGE) == {
            "claude-code", "codex", "cursor", "cline", "continue", "roo",
        }

    @pytest.mark.parametrize("harness", list(COVERAGE))
    def test_absorb_read_cell_is_live(self, harness, tmp_path):
        """Every harness has a real reader that yields >=1 authored candidate."""
        adapter = _build_live_reader(harness, tmp_path / harness)
        assert adapter.harness == harness
        scan = adapter.scan()
        assert len(scan.candidates) >= 1, f"{harness} absorb-read produced nothing"

    @pytest.mark.parametrize("harness", list(COVERAGE))
    def test_mcp_recall_cell_is_exposed(self, harness):
        """Every harness's MCP client can discover + dispatch axiom_memory_recall."""
        pytest.importorskip("mcp")
        from axiom.extensions.builtins.memory import mcp_server

        assert COVERAGE[harness]["mcp_recall"] is True
        names = {t.name for t in mcp_server._TOOLS}
        assert "axiom_memory_recall" in names
        assert "axiom_memory_recall" in mcp_server._HANDLERS

    @pytest.mark.parametrize("harness", list(COVERAGE))
    def test_write_back_cell_writes_its_file(self, harness, tmp_path):
        from axiom.memory.rendering import EpochSnapshot, PreambleEntry
        from axiom.memory.sync.writeback import (
            RULES_FILE_TARGETS,
            MultiTargetWriteBack,
        )

        product = COVERAGE[harness]["write_back_product"]
        expected_rel = COVERAGE[harness]["write_back_file"]
        assert RULES_FILE_TARGETS[product] == expected_rel
        snap = EpochSnapshot(
            session_id="s", epoch=0,
            entries=(PreambleEntry("f1", "prefers ruff"),),
        )
        written = MultiTargetWriteBack(root=tmp_path, products=(product,)).sync(
            snap, cadence="session_boundary",
        )
        target = tmp_path / expected_rel
        assert written == [str(target)]
        assert target.is_file()
        assert "prefers ruff" in target.read_text()

    @pytest.mark.parametrize("harness", list(COVERAGE))
    def test_uncoverable_auto_store_has_recorded_open_question(
        self, harness, open_questions_text
    ):
        """Where an auto store cannot be read, the gap is recorded — not silent."""
        token = COVERAGE[harness]["auto_store_oq"]
        if token is None:
            pytest.skip(f"{harness} has no un-pathed auto store to record")
        assert token in open_questions_text, (
            f"{harness} auto-store gap {token} not recorded in {OPEN_QUESTIONS}"
        )

    def test_stdio_recall_gap_is_recorded(self, open_questions_text):
        """The access-graph contradiction blocking stdio serve is on the record."""
        assert GLOBAL_OQ in open_questions_text

    def test_render_matrix_for_humans(self, capsys):
        """Emit the matrix as a scannable table (visible under -s)."""
        header = f"{'harness':<12} {'absorb-read':<22} {'mcp-recall':<11} {'write-back':<32} auto-store"
        rows = [header, "-" * len(header)]
        for h, cell in COVERAGE.items():
            reader = cell["absorb_read"][1]
            recall = "live" if cell["mcp_recall"] else "--"
            wb = cell["write_back_file"]
            auto = cell["auto_store_oq"] or "n/a"
            rows.append(f"{h:<12} {reader:<22} {recall:<11} {wb:<32} {auto}")
        table = "\n".join(rows)
        print("\n" + table)
        # Structural assertion: one data row per harness, every cell resolved.
        for h, cell in COVERAGE.items():
            assert cell["absorb_read"][0] in {
                "markdown_hierarchy", "structured_store", "rules_files",
            }
            assert cell["write_back_file"]
