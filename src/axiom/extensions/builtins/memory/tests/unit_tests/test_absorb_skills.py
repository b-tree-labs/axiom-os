# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0

"""Tests for the P2 skill functions (ADR-056): ``memory.absorb``,
``memory.conflicts_list``, ``memory.dedup_recluster``.

Logic lives in these skills; the CLI verbs are thin argparse wrappers.
The conflict queue surface is read-only in P2 (list only, no
adjudication verbs) and the re-cluster pass is invocable-only (no
scheduler wiring).
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

PRINCIPAL = "@alice:home"


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
def composition(tmp_path: Path):
    return _make_composition(tmp_path / "node")


@pytest.fixture
def claude_home(tmp_path: Path) -> Path:
    home = tmp_path / "home"
    memdir = home / ".claude" / "projects" / "-Users-alice-proj" / "memory"
    memdir.mkdir(parents=True)
    (home / ".claude" / "CLAUDE.md").write_text("# Rules\n\nBe brief.\n")
    (memdir / "feedback_tdd.md").write_text(
        "---\nname: TDD\ndescription: Tests first\ntype: feedback\n---\n\n"
        "Tests before implementation.\n"
    )
    return home


@pytest.fixture
def codex_home(tmp_path: Path) -> Path:
    home = tmp_path / "home"
    codex = home / ".codex"
    codex.mkdir(parents=True)
    con = sqlite3.connect(codex / "memories_1.sqlite")
    con.execute(
        "CREATE TABLE stage1_outputs (thread_id TEXT PRIMARY KEY, "
        "source_updated_at INTEGER, raw_memory TEXT, rollout_summary TEXT, "
        "generated_at INTEGER, usage_count INTEGER)"
    )
    con.execute(
        "INSERT INTO stage1_outputs VALUES "
        "('t-1', 1752000000, 'Prefers uv over pip.', 'uv preference', "
        "1752000001, 2)"
    )
    con.commit()
    con.close()
    return home


class TestAbsorbSkill:
    def test_absorb_claude_code(self, composition, claude_home):
        from axiom.extensions.builtins.memory.skills.absorb import absorb

        result = absorb({
            "composition": composition,
            "harness": "claude-code",
            "account": "acct-1",
            "principal": PRINCIPAL,
            "home": str(claude_home),
        }, None)
        assert result.ok, result.errors
        assert result.value["imported"] == 2
        assert result.value["harness"] == "claude-code"
        frags = composition.artifact_registry.list(kind="fragment")
        assert len(frags) == 2

    def test_absorb_codex(self, composition, codex_home):
        from axiom.extensions.builtins.memory.skills.absorb import absorb

        result = absorb({
            "composition": composition,
            "harness": "codex",
            "principal": PRINCIPAL,
            "home": str(codex_home),
        }, None)
        assert result.ok, result.errors
        assert result.value["imported"] == 1
        # account defaults to "local" when not given.
        assert result.value["account"] == "local"

    def test_absorb_reabsorb_noop(self, composition, claude_home):
        from axiom.extensions.builtins.memory.skills.absorb import absorb

        params = {
            "composition": composition,
            "harness": "claude-code",
            "principal": PRINCIPAL,
            "home": str(claude_home),
        }
        assert absorb(dict(params), None).value["imported"] == 2
        again = absorb(dict(params), None)
        assert again.value["imported"] == 0
        assert again.value["skipped_echo"] == 2

    def test_absorb_dry_run_writes_nothing(self, composition, claude_home):
        from axiom.extensions.builtins.memory.skills.absorb import absorb

        result = absorb({
            "composition": composition,
            "harness": "claude-code",
            "principal": PRINCIPAL,
            "home": str(claude_home),
            "dry_run": True,
        }, None)
        assert result.ok
        assert result.value["dry_run"] is True
        assert result.value["imported"] == 2  # would import
        assert composition.artifact_registry.list(kind="fragment") == []

    def test_unknown_harness_fails_with_known_list(self, composition):
        from axiom.extensions.builtins.memory.skills.absorb import absorb

        result = absorb({
            "composition": composition,
            "harness": "clippy",
            "principal": PRINCIPAL,
        }, None)
        assert not result.ok
        assert any("clippy" in e for e in result.errors)
        assert any("claude-code" in e for e in result.errors)

    def test_missing_principal_fails(self, composition, claude_home):
        from axiom.extensions.builtins.memory.skills.absorb import absorb

        result = absorb({
            "composition": composition,
            "harness": "claude-code",
            "home": str(claude_home),
        }, None)
        assert not result.ok


class TestConflictsListSkill:
    def test_lists_open_conflicts_read_only(self, composition, claude_home):
        from axiom.extensions.builtins.memory.skills.absorb import absorb
        from axiom.extensions.builtins.memory.skills.conflicts_list import (
            conflicts_list,
        )

        params = {
            "composition": composition,
            "harness": "claude-code",
            "principal": PRINCIPAL,
            "home": str(claude_home),
        }
        absorb(dict(params), None)
        # Plant a contradiction in the source and re-absorb.
        topic = (
            claude_home / ".claude" / "projects" / "-Users-alice-proj"
            / "memory" / "feedback_tdd.md"
        )
        topic.write_text(topic.read_text().replace(
            "Tests before implementation.", "Skip tests for prototypes.",
        ))
        absorb(dict(params), None)

        result = conflicts_list({
            "composition": composition, "principal": PRINCIPAL,
        }, None)
        assert result.ok
        assert result.value["count"] == 1
        entry = result.value["conflicts"][0]
        assert entry["status"] == "open"
        assert len(entry["fragment_ids"]) == 2

    def test_empty_queue(self, composition):
        from axiom.extensions.builtins.memory.skills.conflicts_list import (
            conflicts_list,
        )

        result = conflicts_list({"composition": composition}, None)
        assert result.ok
        assert result.value["count"] == 0
        assert result.value["conflicts"] == []


class TestDedupReclusterSkill:
    def _seed_dups(self, composition):
        for text in (
            "prefers dark roast coffee in the morning",
            "prefers dark-roast coffee in the morning",
        ):
            composition.write(
                content={"summary": text, "text": text},
                cognitive_type="semantic",
                principal_id=PRINCIPAL,
                agents={"axi"},
                resources=set(),
            )

    def test_recluster_merges(self, composition):
        from axiom.extensions.builtins.memory.skills.dedup_recluster import (
            dedup_recluster,
        )

        self._seed_dups(composition)
        result = dedup_recluster({
            "composition": composition, "principal": PRINCIPAL,
        }, None)
        assert result.ok, result.errors
        assert result.value["merged"] == 1
        live = composition.artifact_registry.list(kind="fragment")
        assert len(live) == 1

    def test_recluster_dry_run(self, composition):
        from axiom.extensions.builtins.memory.skills.dedup_recluster import (
            dedup_recluster,
        )

        self._seed_dups(composition)
        result = dedup_recluster({
            "composition": composition, "principal": PRINCIPAL,
            "dry_run": True,
        }, None)
        assert result.ok
        assert result.value["merged"] == 1 and result.value["dry_run"] is True
        assert len(composition.artifact_registry.list(kind="fragment")) == 2

    def test_recluster_requires_principal(self, composition):
        from axiom.extensions.builtins.memory.skills.dedup_recluster import (
            dedup_recluster,
        )

        result = dedup_recluster({"composition": composition}, None)
        assert not result.ok
