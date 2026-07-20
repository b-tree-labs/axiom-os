# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0

"""TDD tests for the cluster-1 absorb adapter — markdown-hierarchy
instruction files (ADR-087 D8; survey §1).

First targets: Claude Code (CLAUDE.md hierarchy + auto-memory markdown
dirs), AGENTS.md-convention products, Gemini CLI (GEMINI.md + the
save-memory append section). Live-verified formats (2026-07-13, this
machine): ``~/.claude/projects/<slug>/memory/*.md`` topic files carry
YAML frontmatter (name, description, type); project instruction files
are plain markdown. Gemini CLI was not installed locally — its format
is built to the survey (verify note recorded in docs/working).
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

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


@pytest.fixture
def claude_home(tmp_path: Path) -> Path:
    """A fixture ``$HOME`` with the live-verified Claude Code layout."""
    home = tmp_path / "home"
    claude = home / ".claude"
    memdir = claude / "projects" / "-Users-alice-proj" / "memory"
    memdir.mkdir(parents=True)
    (claude / "CLAUDE.md").write_text(
        "# Global instructions\n\nAlways answer briefly.\n"
    )
    (memdir / "MEMORY.md").write_text(
        "# Project Memory\n\nIndex only — detail lives in topic files.\n"
    )
    (memdir / "feedback_tdd.md").write_text(
        "---\n"
        "name: TDD preference\n"
        "description: User expects test-driven development\n"
        "type: feedback\n"
        "---\n\n"
        "Always implement with TDD.\n"
    )
    return home


@pytest.fixture
def project_root(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    (root / "pkg" / "node_modules" / "dep").mkdir(parents=True)
    (root / "pkg" / ".git").mkdir(parents=True)
    (root / "CLAUDE.md").write_text("# Repo rules\n\nUse spaces not tabs.\n")
    (root / "pkg" / "CLAUDE.md").write_text("# Pkg rules\n\nKeep APIs small.\n")
    # Decoys that the walk must never pick up:
    (root / "pkg" / "node_modules" / "dep" / "CLAUDE.md").write_text("junk")
    (root / "pkg" / ".git" / "CLAUDE.md").write_text("junk")
    (root / "AGENTS.md").write_text("# Agent conventions\n\nBe idempotent.\n")
    return root


def _snapshot(root: Path) -> dict[str, tuple[int, str]]:
    out = {}
    for p in sorted(root.rglob("*")):
        if p.is_file():
            out[str(p)] = (
                p.stat().st_mtime_ns,
                hashlib.sha256(p.read_bytes()).hexdigest(),
            )
    return out


class TestClaudeCodeAdapter:
    def test_scan_finds_instruction_and_memory_layers(self, claude_home):
        from axiom.memory.absorb.markdown_hierarchy import claude_code_adapter

        adapter = claude_code_adapter(account=ACCOUNT, home=claude_home)
        assert adapter.harness == "claude-code"
        scan = adapter.scan()
        by_ref = {c.origin.source_ref: c for c in scan.candidates}
        # user-level CLAUDE.md + MEMORY.md + topic file
        assert len(scan.candidates) == 3
        topic = next(
            c for c in scan.candidates
            if c.origin.source_ref.endswith("feedback_tdd.md")
        )
        # Frontmatter parsed into content fields.
        assert topic.content["summary"] == "User expects test-driven development"
        assert topic.content["fact_kind"] == "feedback"
        assert "Always implement with TDD." in topic.content["text"]
        assert topic.content["layer"] == "auto_memory"
        assert topic.cognitive_type == "semantic"
        # Instruction file is the authored layer.
        instr = next(
            c for c in scan.candidates
            if c.origin.source_ref.endswith(".claude/CLAUDE.md")
        )
        assert instr.content["layer"] == "authored"
        assert "Always answer briefly." in instr.content["text"]
        # Origin coordinate fully stamped.
        for c in by_ref.values():
            assert c.origin.harness == "claude-code"
            assert c.origin.account == ACCOUNT
            assert c.origin.imported_at

    def test_project_hierarchy_walk_skips_junk_dirs(self, project_root):
        from axiom.memory.absorb.markdown_hierarchy import claude_code_adapter

        adapter = claude_code_adapter(
            account=ACCOUNT, home=project_root / "nohome",
            project_roots=[project_root],
        )
        scan = adapter.scan()
        refs = {c.origin.source_ref for c in scan.candidates}
        assert any(r.endswith("repo/CLAUDE.md") for r in refs)
        assert any(r.endswith("pkg/CLAUDE.md") for r in refs)
        assert not any("node_modules" in r for r in refs)
        assert not any(".git" in r for r in refs)

    def test_missing_store_degrades_to_skip_record(self, tmp_path):
        from axiom.memory.absorb.markdown_hierarchy import claude_code_adapter

        adapter = claude_code_adapter(account=ACCOUNT, home=tmp_path / "empty")
        scan = adapter.scan()
        assert scan.candidates == []
        assert any(s.reason == "missing" for s in scan.skipped)

    def test_malformed_frontmatter_still_absorbs_body(self, claude_home):
        from axiom.memory.absorb.markdown_hierarchy import claude_code_adapter

        bad = (
            claude_home / ".claude" / "projects" / "-Users-alice-proj"
            / "memory" / "broken.md"
        )
        bad.write_text("---\n: not yaml [\n---\n\nStill a memory.\n")
        adapter = claude_code_adapter(account=ACCOUNT, home=claude_home)
        scan = adapter.scan()
        broken = next(
            c for c in scan.candidates
            if c.origin.source_ref.endswith("broken.md")
        )
        assert "Still a memory." in broken.content["text"]


class TestAgentsMdAdapter:
    def test_scan_agents_md_convention(self, project_root):
        from axiom.memory.absorb.markdown_hierarchy import agents_md_adapter

        adapter = agents_md_adapter(account=ACCOUNT, roots=[project_root])
        assert adapter.harness == "agents-md"
        scan = adapter.scan()
        assert len(scan.candidates) == 1
        cand = scan.candidates[0]
        assert cand.origin.source_ref.endswith("AGENTS.md")
        assert "Be idempotent." in cand.content["text"]
        assert cand.content["layer"] == "authored"


class TestGeminiAdapter:
    def test_scan_authored_plus_added_memories(self, tmp_path):
        from axiom.memory.absorb.markdown_hierarchy import gemini_cli_adapter

        home = tmp_path / "home"
        (home / ".gemini").mkdir(parents=True)
        (home / ".gemini" / "GEMINI.md").write_text(
            "# Gemini rules\n\nPrefer concise answers.\n\n"
            "## Gemini Added Memories\n"
            "- Alice prefers tabs in Go\n"
            "- Standup is at 09:30\n"
        )
        adapter = gemini_cli_adapter(account=ACCOUNT, home=home)
        assert adapter.harness == "gemini-cli"
        scan = adapter.scan()
        layers = [c.content["layer"] for c in scan.candidates]
        assert layers.count("authored") == 1
        assert layers.count("auto_memory") == 2
        added = [
            c for c in scan.candidates if c.content["layer"] == "auto_memory"
        ]
        texts = {c.content["text"] for c in added}
        assert texts == {"Alice prefers tabs in Go", "Standup is at 09:30"}
        # Bullet refs are content-hash scoped, distinct per memory.
        refs = {c.origin.source_ref for c in added}
        assert len(refs) == 2
        # The authored layer never duplicates the added-memories bullets.
        authored = next(
            c for c in scan.candidates if c.content["layer"] == "authored"
        )
        assert "Prefer concise answers." in authored.content["text"]
        assert "Alice prefers tabs in Go" not in authored.content["text"]


class TestClusterOneGate:
    """Acceptance-gate items exercised through the real adapter."""

    def test_absorb_reabsorb_is_noop_and_sources_untouched(
        self, claude_home, tmp_path
    ):
        from axiom.memory.absorb.importer import import_candidates
        from axiom.memory.absorb.markdown_hierarchy import claude_code_adapter

        composition = _make_composition(tmp_path / "node")
        adapter = claude_code_adapter(account=ACCOUNT, home=claude_home)
        before = _snapshot(claude_home)

        first = import_candidates(
            composition, adapter.scan().candidates, principal=PRINCIPAL,
        )
        assert first.imported == 3
        second = import_candidates(
            composition, adapter.scan().candidates, principal=PRINCIPAL,
        )
        assert second.imported == 0
        assert second.skipped_echo == 3

        # Read-only guarantee: mtime + content hash unchanged (gate).
        assert _snapshot(claude_home) == before

    def test_edited_source_is_kept_both_and_queued(self, claude_home, tmp_path):
        from axiom.memory.absorb.importer import import_candidates
        from axiom.memory.absorb.markdown_hierarchy import claude_code_adapter
        from axiom.memory.dedup import list_conflicts

        composition = _make_composition(tmp_path / "node")
        adapter = claude_code_adapter(account=ACCOUNT, home=claude_home)
        import_candidates(
            composition, adapter.scan().candidates, principal=PRINCIPAL,
        )
        topic = (
            claude_home / ".claude" / "projects" / "-Users-alice-proj"
            / "memory" / "feedback_tdd.md"
        )
        topic.write_text(topic.read_text().replace(
            "Always implement with TDD.", "TDD is optional for spikes.",
        ))
        report = import_candidates(
            composition, adapter.scan().candidates, principal=PRINCIPAL,
        )
        assert report.imported == 1
        assert report.conflicts_queued == 1
        assert len(list_conflicts(composition, principal=PRINCIPAL)) == 1

    def test_per_source_extraction_round_trip(self, claude_home, tmp_path):
        from axiom.memory.absorb.extract import extract_by_source
        from axiom.memory.absorb.importer import import_candidates
        from axiom.memory.absorb.markdown_hierarchy import claude_code_adapter

        composition = _make_composition(tmp_path / "node")
        adapter = claude_code_adapter(account=ACCOUNT, home=claude_home)
        report = import_candidates(
            composition, adapter.scan().candidates, principal=PRINCIPAL,
        )
        got = extract_by_source(
            composition, harness="claude-code", account=ACCOUNT,
        )
        assert {f.id for f in got} == set(report.fragment_ids)
