# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0

"""TDD tests for the cluster-3 absorb adapter — vector/passage stores
(ADR-087 D8; survey §3).

Letta self-hosted first: archival passages with rich per-passage
metadata + core blocks (persona/human). Built to Letta's documented
self-hosted SQLite shape (not installed locally — verify note in
docs/working): passage tables named ``agent_passages`` /
``source_passages`` (``passages`` in older releases), core-memory
``block`` rows labeled persona/human.

Provenance-mapping law: the passage's own id becomes ``source_ref``,
its timestamps/agent ride into content — and stored **embeddings never
port** (they are disposable projections per D6; text is authoritative).
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

PRINCIPAL = "@alice:home"
ACCOUNT = "letta-local"


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
def letta_db(tmp_path: Path) -> Path:
    db = tmp_path / "letta" / "sqlite.db"
    db.parent.mkdir(parents=True)
    con = sqlite3.connect(db)
    con.execute(
        "CREATE TABLE agent_passages (id TEXT PRIMARY KEY, text TEXT, "
        "embedding BLOB, embedding_config TEXT, metadata_ TEXT, "
        "created_at TEXT, agent_id TEXT, organization_id TEXT)"
    )
    con.executemany(
        "INSERT INTO agent_passages (id, text, embedding, metadata_, "
        "created_at, agent_id) VALUES (?, ?, ?, ?, ?, ?)",
        [
            ("p-1", "Alice deploys releases from tags only.",
             b"\x00\x01", json.dumps({"topic": "release"}),
             "2026-06-01T10:00:00+00:00", "agent-7"),
            ("p-2", "The staging cluster lives in us-east.",
             b"\x00\x02", None, "2026-06-02T11:00:00+00:00", "agent-7"),
        ],
    )
    con.execute(
        "CREATE TABLE block (id TEXT PRIMARY KEY, value TEXT, "
        "label TEXT, metadata_ TEXT)"
    )
    con.executemany(
        "INSERT INTO block (id, value, label) VALUES (?, ?, ?)",
        [
            ("b-1", "Alice is a backend developer.", "human"),
            ("b-2", "I am a careful pair-programming agent.", "persona"),
        ],
    )
    con.commit()
    con.close()
    return db


class TestLettaAdapter:
    def test_passages_with_provenance_mapping(self, letta_db):
        from axiom.memory.absorb.passage_store import letta_adapter

        adapter = letta_adapter(account=ACCOUNT, db_path=letta_db)
        assert adapter.harness == "letta"
        scan = adapter.scan()
        passages = [
            c for c in scan.candidates
            if c.content["fact_kind"] == "letta_passage"
        ]
        assert len(passages) == 2
        p1 = next(p for p in passages if "tags only" in p.content["text"])
        assert p1.origin.source_ref.endswith("agent_passages/p-1")
        assert p1.origin.account == ACCOUNT
        assert p1.content["agent_id"] == "agent-7"
        assert p1.content["event_time"] == "2026-06-01T10:00:00+00:00"
        assert p1.content["metadata"] == {"topic": "release"}
        assert p1.cognitive_type == "semantic"

    def test_embeddings_never_ride_into_content(self, letta_db):
        from axiom.memory.absorb.passage_store import letta_adapter

        scan = letta_adapter(account=ACCOUNT, db_path=letta_db).scan()
        for cand in scan.candidates:
            blob = json.dumps(cand.content, default=str)
            assert "embedding" not in blob

    def test_blocks_become_core_fragments(self, letta_db):
        from axiom.memory.absorb.passage_store import letta_adapter

        scan = letta_adapter(account=ACCOUNT, db_path=letta_db).scan()
        blocks = [
            c for c in scan.candidates
            if c.content["fact_kind"] == "letta_block"
        ]
        assert len(blocks) == 2
        assert all(b.cognitive_type == "core" for b in blocks)
        labels = {b.content["label"] for b in blocks}
        assert labels == {"human", "persona"}

    def test_legacy_passages_table_name_still_reads(self, tmp_path):
        """Older Letta releases used a single ``passages`` table."""
        from axiom.memory.absorb.passage_store import letta_adapter

        db = tmp_path / "legacy.db"
        con = sqlite3.connect(db)
        con.execute(
            "CREATE TABLE passages (id TEXT PRIMARY KEY, text TEXT, "
            "created_at TEXT)"
        )
        con.execute(
            "INSERT INTO passages VALUES "
            "('p-9', 'Legacy archival fact.', '2026-01-01T00:00:00+00:00')"
        )
        con.commit()
        con.close()
        scan = letta_adapter(account=ACCOUNT, db_path=db).scan()
        assert len(scan.candidates) == 1
        assert scan.candidates[0].origin.source_ref.endswith("passages/p-9")

    def test_missing_store_degrades(self, tmp_path):
        from axiom.memory.absorb.passage_store import letta_adapter

        scan = letta_adapter(
            account=ACCOUNT, db_path=tmp_path / "absent.db"
        ).scan()
        assert scan.candidates == []
        assert any(s.reason == "missing" for s in scan.skipped)

    def test_unknown_schema_degrades_to_skip(self, tmp_path):
        from axiom.memory.absorb.passage_store import letta_adapter

        db = tmp_path / "weird.db"
        con = sqlite3.connect(db)
        con.execute("CREATE TABLE something_else (x TEXT)")
        con.commit()
        con.close()
        scan = letta_adapter(account=ACCOUNT, db_path=db).scan()
        assert scan.candidates == []
        assert any("no known" in s.reason for s in scan.skipped)

    def test_reabsorb_noop_and_source_untouched(self, letta_db, tmp_path):
        import hashlib

        from axiom.memory.absorb.importer import import_candidates
        from axiom.memory.absorb.passage_store import letta_adapter

        composition = _make_composition(tmp_path / "node")
        adapter = letta_adapter(account=ACCOUNT, db_path=letta_db)
        before = (
            letta_db.stat().st_mtime_ns,
            hashlib.sha256(letta_db.read_bytes()).hexdigest(),
        )
        first = import_candidates(
            composition, adapter.scan().candidates, principal=PRINCIPAL,
        )
        assert first.imported == 4
        second = import_candidates(
            composition, adapter.scan().candidates, principal=PRINCIPAL,
        )
        assert second.imported == 0 and second.skipped_echo == 4
        after = (
            letta_db.stat().st_mtime_ns,
            hashlib.sha256(letta_db.read_bytes()).hexdigest(),
        )
        assert after == before
