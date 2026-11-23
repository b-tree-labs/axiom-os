# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""TDD tests for P4 write-back fallbacks (ADR-087 D8, PRD F6, scope item 5).

The P4-owned half of D8: AGENTS.md remains the primary write-back target
(P3), PLUS per-product rules-file fallbacks from the harness survey
(.clinerules, .continue/rules, .roo/rules, CONVENTIONS.md, …). Reuses the
P3 :class:`InstructionFileWriteBack` cadence guard, idempotent markered
blocks, and no-op-writes-nothing — so the byte-identical + no-op proofs
carry over to every new fallback target.
"""

from __future__ import annotations

import pytest

from axiom.memory.rendering import (
    EPOCH_ROLLOVER,
    SESSION_BOUNDARY,
    EpochSnapshot,
    PreambleEntry,
    WriteBackRefused,
    render_agents_md_block,
)
from axiom.memory.sync.writeback import (
    FALLBACK_TARGETS,
    PRIMARY_TARGET,
    RULES_FILE_TARGETS,
    MultiTargetWriteBack,
)


def _snap(*entries) -> EpochSnapshot:
    return EpochSnapshot(
        session_id="s", epoch=0,
        entries=tuple(PreambleEntry(fid, txt) for fid, txt in entries),
    )


ALL = (PRIMARY_TARGET, *FALLBACK_TARGETS)


class TestSurveyTargets:
    def test_agents_md_is_primary(self):
        assert PRIMARY_TARGET == "agents_md"
        assert RULES_FILE_TARGETS["agents_md"] == "AGENTS.md"

    def test_survey_fallbacks_present(self):
        # The rules files the survey + loop prompt name explicitly.
        rel = set(RULES_FILE_TARGETS.values())
        assert ".clinerules" in rel
        assert "CONVENTIONS.md" in rel
        assert any(p.startswith(".continue/rules") for p in rel)
        assert any(p.startswith(".roo/rules") for p in rel)
        assert any(p.startswith(".cursor/rules") for p in rel)
        assert "CLAUDE.md" in rel


class TestMultiTargetWriteBack:
    def test_writes_primary_plus_configured_fallbacks(self, tmp_path):
        wb = MultiTargetWriteBack(
            root=tmp_path, products=("agents_md", "cline", "continue", "aider"),
        )
        written = wb.sync(_snap(("f1", "prefers ruff")), cadence=SESSION_BOUNDARY)
        assert len(written) == 4
        assert (tmp_path / "AGENTS.md").exists()
        assert (tmp_path / ".clinerules").exists()
        assert (tmp_path / ".continue" / "rules").exists() or any(
            (tmp_path / ".continue" / "rules").glob("*")
        )
        assert (tmp_path / "CONVENTIONS.md").exists()
        for name in ("AGENTS.md", ".clinerules", "CONVENTIONS.md"):
            assert "prefers ruff" in (tmp_path / name).read_text()
            assert "axiom:cross-mem:begin" in (tmp_path / name).read_text()

    def test_nested_fallback_dirs_created(self, tmp_path):
        wb = MultiTargetWriteBack(root=tmp_path, products=("continue", "roo"))
        wb.sync(_snap(("f1", "note")), cadence=SESSION_BOUNDARY)
        cont = tmp_path / RULES_FILE_TARGETS["continue"]
        roo = tmp_path / RULES_FILE_TARGETS["roo"]
        assert cont.exists() and "note" in cont.read_text()
        assert roo.exists() and "note" in roo.read_text()

    def test_default_is_primary_only(self, tmp_path):
        wb = MultiTargetWriteBack(root=tmp_path)
        written = wb.sync(_snap(("f1", "x")), cadence=SESSION_BOUNDARY)
        assert written == [str(tmp_path / "AGENTS.md")]


class TestReusedP3Proofs:
    def test_byte_identical_block_across_targets(self, tmp_path):
        snap = _snap(("f1", "prefers ruff"), ("f2", "always TDD"))
        wb = MultiTargetWriteBack(root=tmp_path, products=("agents_md", "cline"))
        wb.sync(snap, cadence=SESSION_BOUNDARY)
        expected = render_agents_md_block(snap)
        for name in ("AGENTS.md", ".clinerules"):
            body = (tmp_path / name).read_text()
            assert expected in body

    def test_cursor_write_back_block_is_byte_identical(self, tmp_path):
        # Cursor's directory-style rules convention gets the same managed block,
        # byte-for-byte, inside .cursor/rules/axiom-memory.md — the write side
        # the A2 read-side cursor_adapter is symmetric with.
        snap = _snap(("f1", "prefers ruff"), ("f2", "always TDD"))
        wb = MultiTargetWriteBack(root=tmp_path, products=("cursor",))
        written = wb.sync(snap, cadence=SESSION_BOUNDARY)
        cursor_file = tmp_path / RULES_FILE_TARGETS["cursor"]
        assert cursor_file.name == "axiom-memory.md"
        assert written == [str(cursor_file)]
        assert render_agents_md_block(snap) in cursor_file.read_text()

    def test_no_op_writes_nothing(self, tmp_path):
        snap = _snap(("f1", "prefers ruff"))
        wb = MultiTargetWriteBack(root=tmp_path, products=("agents_md", "cline"))
        wb.sync(snap, cadence=SESSION_BOUNDARY)
        mtimes = {
            n: (tmp_path / n).stat().st_mtime_ns for n in ("AGENTS.md", ".clinerules")
        }
        # Re-sync identical content: no writes, no mtime change.
        written = wb.sync(snap, cadence=EPOCH_ROLLOVER)
        assert written == []
        for n, m in mtimes.items():
            assert (tmp_path / n).stat().st_mtime_ns == m

    def test_mid_session_cadence_refused_on_every_target(self, tmp_path):
        wb = MultiTargetWriteBack(root=tmp_path, products=("agents_md", "cline"))
        with pytest.raises(WriteBackRefused):
            wb.sync(_snap(("f1", "x")), cadence="mid_session")
        # Nothing was written before the refusal.
        assert not (tmp_path / "AGENTS.md").exists()
