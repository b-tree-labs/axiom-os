# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""TDD tests for the read-side rules-file adapters (ADR-087 D8; survey §1;
cross-mem A2 scope item 1).

These make the four write-back-only harnesses bidirectional: Cursor
(``.cursor/rules``), Cline (``.clinerules``), Continue (``.continue/rules``),
and Roo (``.roo/rules``). The readers are symmetric with the write-back targets
in ``memory/sync/writeback.py`` and MUST strip Axiom's own managed block before
emitting a candidate, so a file we wrote out is never read back (echo
suppression, symmetric with P4). Read-only against sources: mtime + hash
unchanged after a scan (asserted here as the D8 gate); re-absorb is a no-op.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from axiom.memory.rendering import (
    EpochSnapshot,
    PreambleEntry,
    render_agents_md_block,
)

PRINCIPAL = "@alice:home"
ACCOUNT = "acct-local"


def _make_composition(base: Path):
    from axiom.artifacts.registry import ArtifactRegistry, SQLiteBackend
    from axiom.memory.access import AccessGraphs
    from axiom.memory.attest import AuditLog
    from axiom.memory.composition import CompositionService
    from axiom.memory.policy import PolicyCoord
    from axiom.memory.trust import TrustGraph
    from axiom.vega.identity.keypair import generate_keypair

    base.mkdir(parents=True, exist_ok=True)
    kp = generate_keypair()
    reg = ArtifactRegistry(backend=SQLiteBackend(base / "artifacts.db"))
    audit = AuditLog(base / "audit.jsonl", signing_keypair=kp)
    return CompositionService(
        artifact_registry=reg,
        audit_log=audit,
        signing_keypair=kp,
        policy_coord=PolicyCoord(global_policy={"write": "private"}),
        access_graphs=AccessGraphs(),
        trust_graph=TrustGraph(),
    )


def _snapshot(root: Path) -> dict[str, tuple[int, str]]:
    out = {}
    for p in sorted(root.rglob("*")):
        if p.is_file():
            out[str(p)] = (
                p.stat().st_mtime_ns,
                hashlib.sha256(p.read_bytes()).hexdigest(),
            )
    return out


def _managed_block(*texts: str) -> str:
    snap = EpochSnapshot(
        session_id="s", epoch=0,
        entries=tuple(PreambleEntry(f"f{i}", t) for i, t in enumerate(texts)),
    )
    return render_agents_md_block(snap)


@pytest.fixture
def cursor_root(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    rules = root / ".cursor" / "rules"
    rules.mkdir(parents=True)
    # Human-authored MDC rule with frontmatter (Cursor's format).
    (rules / "style.mdc").write_text(
        "---\n"
        "description: House style\n"
        "globs: ['**/*.py']\n"
        "---\n\n"
        "Prefer ruff over flake8.\n"
    )
    # Axiom's own managed write-back file — must NOT be re-absorbed.
    (rules / "axiom-memory.md").write_text(
        _managed_block("prefers ruff", "always TDD") + "\n"
    )
    return root


class TestCursorReader:
    def test_absorbs_authored_rules_strips_managed_block(self, cursor_root):
        from axiom.memory.absorb.rules_files import cursor_adapter

        adapter = cursor_adapter(account=ACCOUNT, root=cursor_root)
        assert adapter.harness == "cursor"
        scan = adapter.scan()
        # Only the human file yields a candidate; our managed file strips to "".
        assert len(scan.candidates) == 1
        cand = scan.candidates[0]
        assert cand.origin.source_ref.endswith("style.mdc")
        assert cand.content["layer"] == "authored"
        assert cand.content["fact_kind"] == "rules_file"
        assert "Prefer ruff over flake8." in cand.content["text"]
        # Frontmatter description becomes the summary.
        assert cand.content["summary"] == "House style"
        # Origin coordinate fully stamped.
        assert cand.origin.harness == "cursor"
        assert cand.origin.account == ACCOUNT
        assert cand.origin.imported_at
        # Never re-absorb our own block markers.
        for c in scan.candidates:
            assert "axiom:cross-mem:begin" not in c.content["text"]

    def test_mixed_file_keeps_human_content_drops_our_block(self, tmp_path):
        from axiom.memory.absorb.rules_files import cursor_adapter

        root = tmp_path / "repo"
        rules = root / ".cursor" / "rules"
        rules.mkdir(parents=True)
        (rules / "notes.md").write_text(
            "# My notes\n\nUse spaces not tabs.\n\n"
            + _managed_block("prefers ruff")
            + "\n"
        )
        scan = cursor_adapter(account=ACCOUNT, root=root).scan()
        assert len(scan.candidates) == 1
        text = scan.candidates[0].content["text"]
        assert "Use spaces not tabs." in text
        assert "prefers ruff" not in text
        assert "axiom:cross-mem" not in text

    def test_read_only_sources_untouched(self, cursor_root):
        from axiom.memory.absorb.rules_files import cursor_adapter

        before = _snapshot(cursor_root)
        cursor_adapter(account=ACCOUNT, root=cursor_root).scan()
        assert _snapshot(cursor_root) == before

    def test_missing_store_degrades_to_skip(self, tmp_path):
        from axiom.memory.absorb.rules_files import cursor_adapter

        scan = cursor_adapter(account=ACCOUNT, root=tmp_path / "empty").scan()
        assert scan.candidates == []
        assert any(s.reason == "missing" for s in scan.skipped)


class TestClineReader:
    def test_absorbs_clinerules_file(self, tmp_path):
        from axiom.memory.absorb.rules_files import cline_adapter

        root = tmp_path / "repo"
        root.mkdir()
        (root / ".clinerules").write_text(
            "# Cline rules\n\nRun the linter before every commit.\n"
        )
        adapter = cline_adapter(account=ACCOUNT, root=root)
        assert adapter.harness == "cline"
        scan = adapter.scan()
        assert len(scan.candidates) == 1
        assert scan.candidates[0].origin.source_ref.endswith(".clinerules")
        assert "linter before every commit" in scan.candidates[0].content["text"]

    def test_clinerules_directory_form_is_handled(self, tmp_path):
        """Newer Cline uses a .clinerules/ directory — read it, no new path guessed."""
        from axiom.memory.absorb.rules_files import cline_adapter

        root = tmp_path / "repo"
        rules = root / ".clinerules"
        rules.mkdir(parents=True)
        (rules / "core.md").write_text("Keep functions small.\n")
        (rules / "axiom-memory.md").write_text(_managed_block("note") + "\n")
        scan = cline_adapter(account=ACCOUNT, root=root).scan()
        assert len(scan.candidates) == 1
        assert "Keep functions small." in scan.candidates[0].content["text"]

    def test_only_our_block_yields_nothing(self, tmp_path):
        from axiom.memory.absorb.rules_files import cline_adapter

        root = tmp_path / "repo"
        root.mkdir()
        (root / ".clinerules").write_text(_managed_block("prefers ruff") + "\n")
        scan = cline_adapter(account=ACCOUNT, root=root).scan()
        # Echo suppression: nothing but our own block → no candidate.
        assert scan.candidates == []


class TestContinueReader:
    def test_absorbs_continue_rules_dir(self, tmp_path):
        from axiom.memory.absorb.rules_files import continue_adapter

        root = tmp_path / "repo"
        rules = root / ".continue" / "rules"
        rules.mkdir(parents=True)
        (rules / "python.md").write_text("Type-hint all public functions.\n")
        adapter = continue_adapter(account=ACCOUNT, root=root)
        assert adapter.harness == "continue"
        scan = adapter.scan()
        assert len(scan.candidates) == 1
        assert "Type-hint all public functions." in scan.candidates[0].content["text"]


class TestRooReader:
    def test_absorbs_roo_rules_dir(self, tmp_path):
        from axiom.memory.absorb.rules_files import roo_adapter

        root = tmp_path / "repo"
        rules = root / ".roo" / "rules"
        rules.mkdir(parents=True)
        (rules / "conventions.md").write_text("Commit messages use imperative mood.\n")
        adapter = roo_adapter(account=ACCOUNT, root=root)
        assert adapter.harness == "roo"
        scan = adapter.scan()
        assert len(scan.candidates) == 1
        assert "imperative mood" in scan.candidates[0].content["text"]


class TestRulesReaderGate:
    """Acceptance-gate items exercised through the real adapters + importer."""

    def test_reabsorb_noop_and_sources_untouched(self, cursor_root, tmp_path):
        from axiom.memory.absorb.importer import import_candidates
        from axiom.memory.absorb.rules_files import cursor_adapter

        composition = _make_composition(tmp_path / "node")
        adapter = cursor_adapter(account=ACCOUNT, root=cursor_root)
        before = _snapshot(cursor_root)

        first = import_candidates(
            composition, adapter.scan().candidates, principal=PRINCIPAL,
        )
        assert first.imported == 1
        second = import_candidates(
            composition, adapter.scan().candidates, principal=PRINCIPAL,
        )
        assert second.imported == 0
        assert second.skipped_echo == 1
        # Read-only guarantee (D8 gate): mtime + hash unchanged.
        assert _snapshot(cursor_root) == before

    def test_writeback_then_read_is_echo_suppressed(self, tmp_path):
        """End-to-end symmetry: what write-back writes, the reader never re-absorbs."""
        from axiom.memory.absorb.importer import import_candidates
        from axiom.memory.absorb.rules_files import cursor_adapter
        from axiom.memory.rendering import EpochSnapshot, PreambleEntry
        from axiom.memory.sync.writeback import MultiTargetWriteBack

        root = tmp_path / "repo"
        root.mkdir()
        snap = EpochSnapshot(
            session_id="s", epoch=0,
            entries=(PreambleEntry("f1", "prefers ruff"),),
        )
        MultiTargetWriteBack(root=root, products=("cursor",)).sync(
            snap, cadence="session_boundary",
        )
        assert (root / ".cursor" / "rules" / "axiom-memory.md").exists()

        composition = _make_composition(tmp_path / "node")
        scan = cursor_adapter(account=ACCOUNT, root=root).scan()
        # The only file present is the one we wrote; the reader strips it.
        assert scan.candidates == []
        report = import_candidates(
            composition, scan.candidates, principal=PRINCIPAL,
        )
        assert report.imported == 0
