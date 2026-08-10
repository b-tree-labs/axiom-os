# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tests for axiom.rag.health — domain-agnostic RAG corpus health helper.

The helper underpins ``axi config --status`` so the operator sees not just
"connection configured / missing" but also "corpus populated / empty,
embedding model, recent retrieval quality."  It must never raise — a
missing or unreadable RAG store should yield an empty ``RagHealth``.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from axiom.rag.health import (
    CorpusHealth,
    RagHealth,
    collect_rag_health,
    render_rag_health,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_store(db_path: Path, corpora: dict[str, dict]) -> None:
    """Create a minimal SQLite RAG store at ``db_path`` with seeded data.

    ``corpora`` maps ``corpus_id`` to a payload dict::

        {
            "chunks": int,
            "last_indexed": datetime | None,
            "generation": int,
        }
    """
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.executescript(
        """
        CREATE TABLE documents (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_path TEXT,
            corpus TEXT,
            last_indexed TEXT,
            corpus_generation INTEGER DEFAULT 1,
            chunk_count INTEGER DEFAULT 0
        );
        CREATE TABLE chunks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            corpus TEXT,
            chunk_text TEXT,
            indexed_at TEXT,
            corpus_generation INTEGER DEFAULT 1
        );
        CREATE TABLE retrieval_audit (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            query_text TEXT,
            retrieved_chunks TEXT,
            created_at TEXT
        );
        """
    )
    for corpus, payload in corpora.items():
        chunks = payload.get("chunks", 0)
        last = payload.get("last_indexed")
        gen = payload.get("generation", 1)
        last_iso = last.isoformat() if last else ""
        if chunks > 0:
            conn.execute(
                "INSERT INTO documents (source_path, corpus, last_indexed, "
                "corpus_generation, chunk_count) VALUES (?, ?, ?, ?, ?)",
                (f"/seed/{corpus}.md", corpus, last_iso, gen, chunks),
            )
            for i in range(chunks):
                conn.execute(
                    "INSERT INTO chunks (corpus, chunk_text, indexed_at, "
                    "corpus_generation) VALUES (?, ?, ?, ?)",
                    (corpus, f"chunk-{i}", last_iso, gen),
                )
    conn.commit()
    conn.close()


def _seed_audit(db_path: Path, scores: list[float], hours_ago: float = 1.0) -> None:
    """Append retrieval audit rows; each row's top score is ``scores[i]``."""
    conn = sqlite3.connect(str(db_path))
    when = (datetime.now(UTC) - timedelta(hours=hours_ago)).isoformat()
    for s in scores:
        payload = json.dumps([{"rrf_score": s, "rank": 1}])
        conn.execute(
            "INSERT INTO retrieval_audit (query_text, retrieved_chunks, "
            "created_at) VALUES (?, ?, ?)",
            ("q", payload, when),
        )
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# collect_rag_health behaviour
# ---------------------------------------------------------------------------


class TestCollectRagHealthEmpty:
    def test_no_rag_root_returns_empty(self):
        result = collect_rag_health(rag_root=None)
        assert isinstance(result, RagHealth)
        assert result.corpora == ()
        assert result.total_chunks == 0
        assert result.healthy is False

    def test_missing_path_returns_empty(self, tmp_path):
        missing = tmp_path / "does_not_exist.db"
        result = collect_rag_health(rag_root=missing)
        assert result.corpora == ()
        assert result.total_chunks == 0
        assert result.healthy is False

    def test_empty_directory_returns_empty(self, tmp_path):
        empty_dir = tmp_path / "empty"
        empty_dir.mkdir()
        result = collect_rag_health(rag_root=empty_dir)
        assert result.corpora == ()
        assert result.healthy is False


class TestCollectRagHealthPopulated:
    def test_one_populated_corpus_marks_healthy(self, tmp_path):
        db = tmp_path / "rag.db"
        _make_store(db, {"rag-community": {"chunks": 12,
                                           "last_indexed": datetime(2026, 4, 25, 14, 30, tzinfo=UTC)}})
        health = collect_rag_health(rag_root=db)
        assert health.healthy is True
        assert health.total_chunks == 12
        assert len(health.corpora) == 1
        c = health.corpora[0]
        assert c.corpus_id == "rag-community"
        assert c.chunk_count == 12
        assert c.last_ingested_at is not None
        assert "2026-04-25" in c.last_ingested_at

    def test_mix_of_populated_and_empty(self, tmp_path):
        db = tmp_path / "rag.db"
        _make_store(
            db,
            {
                "rag-community": {"chunks": 5,
                                  "last_indexed": datetime(2026, 4, 1, tzinfo=UTC)},
                "rag-org": {"chunks": 0},
            },
        )
        health = collect_rag_health(rag_root=db)
        assert health.healthy is True
        assert health.total_chunks == 5
        ids = {c.corpus_id for c in health.corpora}
        assert "rag-community" in ids
        # Empty corpora that have no chunks are not in the chunks table — they
        # only appear if the caller passes a `known_corpora` list.  By default
        # we surface every corpus that was actually written to.
        empty = collect_rag_health(rag_root=db,
                                   known_corpora=("rag-community", "rag-org"))
        ids = {c.corpus_id for c in empty.corpora}
        assert ids == {"rag-community", "rag-org"}
        org = next(c for c in empty.corpora if c.corpus_id == "rag-org")
        assert org.chunk_count == 0

    def test_active_generation_picks_max(self, tmp_path):
        db = tmp_path / "rag.db"
        _make_store(
            db,
            {
                "rag-community": {
                    "chunks": 3,
                    "last_indexed": datetime(2026, 4, 20, tzinfo=UTC),
                    "generation": 7,
                },
            },
        )
        health = collect_rag_health(rag_root=db)
        c = health.corpora[0]
        assert c.active_generation == "7"


class TestCollectRagHealthRetrievalScores:
    def test_no_audit_rows_yields_none(self, tmp_path):
        db = tmp_path / "rag.db"
        _make_store(db, {"rag-community": {"chunks": 1,
                                           "last_indexed": datetime(2026, 4, 25, tzinfo=UTC)}})
        health = collect_rag_health(rag_root=db)
        c = health.corpora[0]
        assert c.recent_retrieval_p50_score is None
        assert c.recent_retrieval_count == 0

    def test_recent_audit_rows_yield_p50(self, tmp_path):
        db = tmp_path / "rag.db"
        _make_store(db, {"rag-community": {"chunks": 1,
                                           "last_indexed": datetime(2026, 4, 25, tzinfo=UTC)}})
        _seed_audit(db, [0.5, 0.6, 0.7], hours_ago=1.0)
        health = collect_rag_health(rag_root=db)
        c = health.corpora[0]
        assert c.recent_retrieval_count == 3
        # median of (0.5, 0.6, 0.7) is 0.6
        assert c.recent_retrieval_p50_score == pytest.approx(0.6, abs=0.001)

    def test_old_audit_rows_excluded(self, tmp_path):
        db = tmp_path / "rag.db"
        _make_store(db, {"rag-community": {"chunks": 1,
                                           "last_indexed": datetime(2026, 4, 25, tzinfo=UTC)}})
        _seed_audit(db, [0.9], hours_ago=48.0)  # outside 24h window
        health = collect_rag_health(rag_root=db)
        c = health.corpora[0]
        assert c.recent_retrieval_count == 0
        assert c.recent_retrieval_p50_score is None


# ---------------------------------------------------------------------------
# Tolerance: never raises
# ---------------------------------------------------------------------------


class TestCollectRagHealthTolerance:
    def test_unreadable_path_returns_empty_without_raising(self, tmp_path):
        bogus = tmp_path / "not-a-db"
        bogus.write_text("this is not a sqlite database at all")
        # Must not raise
        result = collect_rag_health(rag_root=bogus)
        assert isinstance(result, RagHealth)
        assert result.corpora == ()
        assert result.healthy is False

    def test_directory_without_db_returns_empty(self, tmp_path):
        (tmp_path / "junk.txt").write_text("hi")
        result = collect_rag_health(rag_root=tmp_path)
        assert isinstance(result, RagHealth)
        assert result.healthy is False

    def test_collect_never_raises_on_garbage(self, tmp_path):
        # A directory we cannot stat cleanly — pass a path that simulates
        # missing parent.
        weird = tmp_path / "nope" / "deep" / "rag.db"
        # Should not raise even though parents don't exist.
        result = collect_rag_health(rag_root=weird)
        assert isinstance(result, RagHealth)


# ---------------------------------------------------------------------------
# render_rag_health output
# ---------------------------------------------------------------------------


class TestRenderRagHealth:
    def test_render_empty(self, capsys):
        render_rag_health(RagHealth(corpora=(), total_chunks=0, healthy=False))
        out = capsys.readouterr().out
        assert "RAG" in out
        # Must not name any domain/site/host-specific terms — domain-agnostic.
        for forbidden in ("nuclear", "NETL", "Rascal", "reactor"):
            assert forbidden.lower() not in out.lower()

    def test_render_populated(self, capsys):
        h = RagHealth(
            corpora=(
                CorpusHealth(
                    corpus_id="rag-community",
                    chunk_count=1234,
                    last_ingested_at="2026-04-25T14:30:00+00:00",
                    embedding_model="text-embedding-3-small",
                    active_generation="3",
                    recent_retrieval_p50_score=0.62,
                    recent_retrieval_count=18,
                ),
                CorpusHealth(
                    corpus_id="rag-org",
                    chunk_count=0,
                    last_ingested_at=None,
                    embedding_model=None,
                    active_generation=None,
                    recent_retrieval_p50_score=None,
                    recent_retrieval_count=0,
                ),
            ),
            total_chunks=1234,
            healthy=True,
        )
        render_rag_health(h)
        out = capsys.readouterr().out
        assert "rag-community" in out
        assert "1,234" in out or "1234" in out
        assert "2026-04-25" in out
        assert "text-embedding-3-small" in out
        assert "0.62" in out
        assert "rag-org" in out
        assert "empty" in out.lower()


# ---------------------------------------------------------------------------
# axi config --status integration
# ---------------------------------------------------------------------------


class TestConfigStatusIntegration:
    def test_show_status_includes_rag_section(self, tmp_path, capsys, monkeypatch):
        from axiom.setup.wizard import SetupWizard

        wizard = SetupWizard(root=tmp_path)
        # Force collect_rag_health to return a known shape so we don't depend
        # on whatever real RAG state the dev box has.
        sentinel = RagHealth(
            corpora=(
                CorpusHealth(
                    corpus_id="rag-community",
                    chunk_count=42,
                    last_ingested_at="2026-04-25T14:30:00+00:00",
                    embedding_model="text-embedding-3-small",
                    active_generation="1",
                    recent_retrieval_p50_score=0.55,
                    recent_retrieval_count=4,
                ),
            ),
            total_chunks=42,
            healthy=True,
        )
        monkeypatch.setattr(
            "axiom.setup.wizard.collect_rag_health", lambda *a, **kw: sentinel
        )
        wizard.show_status()
        out = capsys.readouterr().out
        assert "Configuration Status" in out
        assert "RAG" in out
        assert "rag-community" in out
        assert "42" in out
