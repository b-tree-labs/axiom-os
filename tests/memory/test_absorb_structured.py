# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0

"""TDD tests for the cluster-2 absorb adapter — local structured stores
(ADR-087 D8; survey §2).

Targets: Codex per-app SQLite (live-verified schema 2026-07-13:
``memories_1.sqlite`` → ``stage1_outputs(thread_id, raw_memory,
rollout_summary, usage_count, …)``, ``goals_1.sqlite`` →
``thread_goals(thread_id, objective, status, …)``, ``_sqlx_migrations``
observed — schemas churn), Goose category ``.txt`` files with tag
headers, Hermes ``MEMORY.md``/``USER.md`` §-delimited entries (both
survey-documented; not installed locally).

Cluster-2 law: parse defensively. Schema drift degrades to
skip-with-record, never a crash and never a partial write. App-owned
databases are opened read-only and never written.
"""

from __future__ import annotations

import hashlib
import sqlite3
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


def _snapshot(root: Path) -> dict[str, tuple[int, str]]:
    out = {}
    for p in sorted(root.rglob("*")):
        if p.is_file():
            out[str(p)] = (
                p.stat().st_mtime_ns,
                hashlib.sha256(p.read_bytes()).hexdigest(),
            )
    return out


@pytest.fixture
def codex_home(tmp_path: Path) -> Path:
    """A ``$HOME`` with the live-verified ``~/.codex`` SQLite layout."""
    home = tmp_path / "home"
    codex = home / ".codex"
    codex.mkdir(parents=True)

    mem = sqlite3.connect(codex / "memories_1.sqlite")
    mem.execute(
        "CREATE TABLE _sqlx_migrations (version BIGINT PRIMARY KEY, "
        "description TEXT NOT NULL, installed_on TIMESTAMP, success BOOLEAN, "
        "checksum BLOB, execution_time BIGINT)"
    )
    mem.execute(
        "CREATE TABLE stage1_outputs (thread_id TEXT PRIMARY KEY, "
        "source_updated_at INTEGER NOT NULL, raw_memory TEXT NOT NULL, "
        "rollout_summary TEXT NOT NULL, rollout_slug TEXT, "
        "generated_at INTEGER NOT NULL, usage_count INTEGER, "
        "last_usage INTEGER, selected_for_phase2 INTEGER NOT NULL DEFAULT 0, "
        "selected_for_phase2_source_updated_at INTEGER)"
    )
    mem.executemany(
        "INSERT INTO stage1_outputs (thread_id, source_updated_at, "
        "raw_memory, rollout_summary, generated_at, usage_count) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        [
            ("t-1", 1752000000, "User prefers uv over pip.",
             "Package-manager preference", 1752000001, 3),
            ("t-2", 1752000100, "CI must stay green before merges.",
             "CI discipline", 1752000101, 1),
        ],
    )
    mem.commit()
    mem.close()

    goals = sqlite3.connect(codex / "goals_1.sqlite")
    goals.execute(
        "CREATE TABLE thread_goals (thread_id TEXT PRIMARY KEY NOT NULL, "
        "goal_id TEXT NOT NULL, objective TEXT NOT NULL, "
        "status TEXT NOT NULL, token_budget INTEGER, "
        "tokens_used INTEGER NOT NULL DEFAULT 0, "
        "time_used_seconds INTEGER NOT NULL DEFAULT 0, "
        "created_at_ms INTEGER NOT NULL, updated_at_ms INTEGER NOT NULL)"
    )
    goals.execute(
        "INSERT INTO thread_goals (thread_id, goal_id, objective, status, "
        "created_at_ms, updated_at_ms) VALUES "
        "('t-9', 'g-1', 'Ship the importer', 'active', "
        "1752000000000, 1752000500000)"
    )
    goals.commit()
    goals.close()
    return home


@pytest.fixture
def goose_base(tmp_path: Path) -> Path:
    base = tmp_path / "goose-memory"
    base.mkdir(parents=True)
    (base / "development.txt").write_text(
        "# tooling python\nUse uv for all Python installs.\n"
        "\n"
        "Run linters before committing.\n"
    )
    (base / "personal.txt").write_text("# coffee\nDrinks flat whites.\n")
    return base


@pytest.fixture
def hermes_home(tmp_path: Path) -> Path:
    home = tmp_path / "home"
    memories = home / ".hermes" / "memories"
    memories.mkdir(parents=True)
    (memories / "MEMORY.md").write_text(
        "§ Editor setup\nUses helix with a custom theme.\n"
        "§ Testing\nPrefers property-based tests.\n"
    )
    (memories / "USER.md").write_text(
        "§ Identity\nAlice, backend developer.\n"
    )
    return home


class TestCodexAdapter:
    def test_scan_reads_memories_and_goals(self, codex_home):
        from axiom.memory.absorb.structured_store import codex_adapter

        adapter = codex_adapter(account=ACCOUNT, home=codex_home)
        assert adapter.harness == "codex"
        scan = adapter.scan()
        assert len(scan.candidates) == 3
        mems = [
            c for c in scan.candidates
            if c.content.get("fact_kind") == "codex_memory"
        ]
        assert {m.content["text"] for m in mems} == {
            "User prefers uv over pip.", "CI must stay green before merges.",
        }
        m1 = next(m for m in mems if m.content["text"].startswith("User"))
        assert m1.content["summary"] == "Package-manager preference"
        assert m1.origin.source_ref.endswith("stage1_outputs/t-1")
        assert m1.cognitive_type == "semantic"
        goal = next(
            c for c in scan.candidates
            if c.content.get("fact_kind") == "thread_goal"
        )
        assert goal.cognitive_type == "episodic"
        assert goal.content["event_time"].startswith("2025") or (
            goal.content["event_time"].startswith("2026")
        )
        assert goal.content["summary"] == "Ship the importer"

    def test_missing_store_degrades(self, tmp_path):
        from axiom.memory.absorb.structured_store import codex_adapter

        scan = codex_adapter(account=ACCOUNT, home=tmp_path / "no").scan()
        assert scan.candidates == []
        assert any(s.reason == "missing" for s in scan.skipped)

    def test_schema_drift_skips_table_never_crashes(self, codex_home):
        """A vendor migration renames a table: skip it, absorb the rest."""
        from axiom.memory.absorb.structured_store import codex_adapter

        db = codex_home / ".codex" / "memories_1.sqlite"
        con = sqlite3.connect(db)
        con.execute("ALTER TABLE stage1_outputs RENAME TO stage2_outputs")
        con.commit()
        con.close()

        scan = codex_adapter(account=ACCOUNT, home=codex_home).scan()
        # Goals still absorbed; renamed table skipped with a record.
        kinds = {c.content.get("fact_kind") for c in scan.candidates}
        assert kinds == {"thread_goal"}
        assert any("stage1_outputs" in s.source for s in scan.skipped)

    def test_corrupt_database_skips_never_crashes(self, codex_home):
        from axiom.memory.absorb.structured_store import codex_adapter

        (codex_home / ".codex" / "memories_1.sqlite").write_bytes(
            b"not a database"
        )
        scan = codex_adapter(account=ACCOUNT, home=codex_home).scan()
        kinds = {c.content.get("fact_kind") for c in scan.candidates}
        assert kinds == {"thread_goal"}
        assert any("memories_1.sqlite" in s.source for s in scan.skipped)

    def test_absorb_reabsorb_noop_and_sources_untouched(
        self, codex_home, tmp_path
    ):
        from axiom.memory.absorb.importer import import_candidates
        from axiom.memory.absorb.structured_store import codex_adapter

        composition = _make_composition(tmp_path / "node")
        adapter = codex_adapter(account=ACCOUNT, home=codex_home)
        before = _snapshot(codex_home)
        first = import_candidates(
            composition, adapter.scan().candidates, principal=PRINCIPAL,
        )
        assert first.imported == 3
        second = import_candidates(
            composition, adapter.scan().candidates, principal=PRINCIPAL,
        )
        assert second.imported == 0 and second.skipped_echo == 3
        assert _snapshot(codex_home) == before


class TestGooseAdapter:
    def test_scan_category_files_with_tag_headers(self, goose_base):
        from axiom.memory.absorb.structured_store import goose_adapter

        adapter = goose_adapter(account=ACCOUNT, base=goose_base)
        assert adapter.harness == "goose"
        scan = adapter.scan()
        assert len(scan.candidates) == 3
        dev = [
            c for c in scan.candidates
            if c.content["category"] == "development"
        ]
        assert len(dev) == 2
        tagged = next(
            c for c in dev if "uv" in c.content["text"]
        )
        assert tagged.content["tags"] == ["tooling", "python"]
        untagged = next(
            c for c in dev if "linters" in c.content["text"]
        )
        assert untagged.content["tags"] == []
        # Content-hash scoped refs: distinct per entry.
        refs = {c.origin.source_ref for c in scan.candidates}
        assert len(refs) == 3

    def test_reabsorb_noop(self, goose_base, tmp_path):
        from axiom.memory.absorb.importer import import_candidates
        from axiom.memory.absorb.structured_store import goose_adapter

        composition = _make_composition(tmp_path / "node")
        adapter = goose_adapter(account=ACCOUNT, base=goose_base)
        assert import_candidates(
            composition, adapter.scan().candidates, principal=PRINCIPAL,
        ).imported == 3
        again = import_candidates(
            composition, adapter.scan().candidates, principal=PRINCIPAL,
        )
        assert again.imported == 0 and again.skipped_echo == 3


class TestHermesAdapter:
    def test_scan_section_delimited_entries(self, hermes_home):
        from axiom.memory.absorb.structured_store import hermes_adapter

        adapter = hermes_adapter(account=ACCOUNT, home=hermes_home)
        assert adapter.harness == "hermes"
        scan = adapter.scan()
        assert len(scan.candidates) == 3
        summaries = {c.content["summary"] for c in scan.candidates}
        assert summaries == {"Editor setup", "Testing", "Identity"}
        editor = next(
            c for c in scan.candidates
            if c.content["summary"] == "Editor setup"
        )
        assert "helix" in editor.content["text"]
        assert editor.origin.source_ref.split("#")[0].endswith("MEMORY.md")

    def test_reabsorb_noop_and_sources_untouched(self, hermes_home, tmp_path):
        from axiom.memory.absorb.importer import import_candidates
        from axiom.memory.absorb.structured_store import hermes_adapter

        composition = _make_composition(tmp_path / "node")
        adapter = hermes_adapter(account=ACCOUNT, home=hermes_home)
        before = _snapshot(hermes_home)
        assert import_candidates(
            composition, adapter.scan().candidates, principal=PRINCIPAL,
        ).imported == 3
        again = import_candidates(
            composition, adapter.scan().candidates, principal=PRINCIPAL,
        )
        assert again.imported == 0 and again.skipped_echo == 3
        assert _snapshot(hermes_home) == before
