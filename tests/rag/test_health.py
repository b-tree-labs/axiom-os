# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tests for axiom.rag.health — domain-agnostic RAG corpus health helper.

The helper underpins ``axi config --status`` so the operator sees not just
"connection configured / missing" but also "corpus populated / empty,
embedding model, recent retrieval quality."  It must never raise — a missing
or unreachable store should yield an empty ``RagHealth``.  It reads the
Postgres retrieval store (ADR-174), so these run against a real database, a
fresh one per test: the audit log is global to a database, and a shared one
would bleed one test's retrievals into another's median.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

from axiom.rag.health import (
    CorpusHealth,
    RagHealth,
    collect_rag_health,
    render_rag_health,
)
from tests.test_axiom_names_no_domain import assert_rendered_text_names_no_domain

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def store_url(fresh_postgres):
    """A real retrieval schema (created by the store itself) in a fresh database."""
    from axiom.rag.store import RAGStore

    store = RAGStore(
        fresh_postgres.replace("postgresql+psycopg2://", "postgresql://"), ensure_schema=True
    )
    store.connect()
    store.close()
    return fresh_postgres.replace("postgresql+psycopg2://", "postgresql://")


def _connect(url):
    import psycopg2

    conn = psycopg2.connect(url)
    conn.autocommit = True
    return conn


def _make_store(url: str, corpora: dict[str, dict]) -> None:
    """Seed ``documents`` and ``chunks`` in the store's real schema.

    ``corpora`` maps ``corpus_id`` to ``{"chunks": int, "last_indexed": datetime | None,
    "generation": int}``.
    """
    conn = _connect(url)
    with conn.cursor() as cur:
        for corpus, payload in corpora.items():
            chunks = payload.get("chunks", 0)
            last = payload.get("last_indexed") or datetime.now(UTC)
            gen = payload.get("generation", 1)
            if chunks > 0:
                cur.execute(
                    "INSERT INTO documents (source_path, corpus, last_indexed, corpus_generation, chunk_count) "
                    "VALUES (%s, %s, %s, %s, %s)",
                    (f"/seed/{corpus}.md", corpus, last, gen, chunks),
                )
                for i in range(chunks):
                    cur.execute(
                        "INSERT INTO chunks (source_path, chunk_text, chunk_index, corpus, corpus_generation) "
                        "VALUES (%s, %s, %s, %s, %s)",
                        (f"/seed/{corpus}.md", f"chunk-{i}", i, corpus, gen),
                    )
    conn.close()


def _seed_audit(url: str, scores: list[float], hours_ago: float = 1.0) -> None:
    """Append retrieval audit rows; each row's top score is ``scores[i]``."""
    when = datetime.now(UTC) - timedelta(hours=hours_ago)
    conn = _connect(url)
    with conn.cursor() as cur:
        for s in scores:
            cur.execute(
                "INSERT INTO retrieval_audit (query_text, query_hash, retrieved_chunks, created_at) "
                "VALUES (%s, %s, %s, %s)",
                ("q", "h", json.dumps([{"rrf_score": s, "rank": 1}]), when),
            )
    conn.close()


# ---------------------------------------------------------------------------
# collect_rag_health behaviour
# ---------------------------------------------------------------------------


class TestCollectRagHealthEmpty:
    def test_no_store_configured_returns_empty(self, monkeypatch):
        monkeypatch.delenv("DATABASE_URL", raising=False)
        monkeypatch.setattr("axiom.rag.health.default_database_url", lambda: "")
        result = collect_rag_health()
        assert isinstance(result, RagHealth)
        assert result.corpora == ()
        assert result.total_chunks == 0
        assert result.healthy is False

    def test_a_non_postgres_url_returns_empty(self):
        # A SQLite file is not a runtime store (ADR-174); it reads as "no store", not as an error.
        result = collect_rag_health("sqlite:///x.db")
        assert result.corpora == () and result.healthy is False

    def test_an_empty_database_returns_empty(self, fresh_postgres):
        result = collect_rag_health(
            fresh_postgres.replace("postgresql+psycopg2://", "postgresql://")
        )
        assert result.corpora == ()
        assert result.healthy is False


class TestCollectRagHealthPopulated:
    def test_one_populated_corpus_marks_healthy(self, store_url):
        _make_store(
            store_url,
            {
                "rag-community": {
                    "chunks": 12,
                    "last_indexed": datetime(2026, 4, 25, 14, 30, tzinfo=UTC),
                }
            },
        )
        health = collect_rag_health(store_url)
        assert health.healthy is True
        assert health.total_chunks == 12
        assert len(health.corpora) == 1
        c = health.corpora[0]
        assert c.corpus_id == "rag-community"
        assert c.chunk_count == 12
        assert c.last_ingested_at is not None
        assert "2026-04-25" in c.last_ingested_at

    def test_mix_of_populated_and_empty(self, store_url):
        _make_store(
            store_url,
            {
                "rag-community": {"chunks": 5, "last_indexed": datetime(2026, 4, 1, tzinfo=UTC)},
                "rag-org": {"chunks": 0},
            },
        )
        health = collect_rag_health(store_url)
        assert health.healthy is True
        assert health.total_chunks == 5
        assert "rag-community" in {c.corpus_id for c in health.corpora}
        # An empty corpus has no rows, so it only appears if the caller names it.
        empty = collect_rag_health(store_url, known_corpora=("rag-community", "rag-org"))
        assert {c.corpus_id for c in empty.corpora} == {"rag-community", "rag-org"}
        org = next(c for c in empty.corpora if c.corpus_id == "rag-org")
        assert org.chunk_count == 0

    def test_active_generation_picks_max(self, store_url):
        _make_store(
            store_url,
            {
                "rag-community": {
                    "chunks": 3,
                    "last_indexed": datetime(2026, 4, 20, tzinfo=UTC),
                    "generation": 7,
                },
            },
        )
        assert collect_rag_health(store_url).corpora[0].active_generation == "7"


class TestCollectRagHealthRetrievalScores:
    def test_no_audit_rows_yields_none(self, store_url):
        _make_store(store_url, {"rag-community": {"chunks": 1}})
        c = collect_rag_health(store_url).corpora[0]
        assert c.recent_retrieval_p50_score is None
        assert c.recent_retrieval_count == 0

    def test_recent_audit_rows_yield_p50(self, store_url):
        _make_store(store_url, {"rag-community": {"chunks": 1}})
        _seed_audit(store_url, [0.5, 0.6, 0.7], hours_ago=1.0)
        c = collect_rag_health(store_url).corpora[0]
        assert c.recent_retrieval_count == 3
        assert c.recent_retrieval_p50_score == pytest.approx(0.6, abs=0.001)

    def test_old_audit_rows_excluded(self, store_url):
        _make_store(store_url, {"rag-community": {"chunks": 1}})
        _seed_audit(store_url, [0.9], hours_ago=48.0)  # outside the 24h window
        c = collect_rag_health(store_url).corpora[0]
        assert c.recent_retrieval_count == 0
        assert c.recent_retrieval_p50_score is None


# ---------------------------------------------------------------------------
# Tolerance: never raises
# ---------------------------------------------------------------------------


class TestCollectRagHealthTolerance:
    def test_an_unreachable_server_returns_empty_without_raising(self):
        result = collect_rag_health("postgresql://nobody:x@127.0.0.1:1/none")
        assert isinstance(result, RagHealth)
        assert result.corpora == ()
        assert result.healthy is False

    def test_garbage_is_tolerated(self):
        for junk in ("", "not a url", "postgresql://", "http://example.invalid/x"):
            assert isinstance(collect_rag_health(junk), RagHealth)

    def test_a_database_without_the_retrieval_tables_returns_empty(self, fresh_postgres):
        # Reachable, but nothing here was ever indexed: no tables, no error.
        url = fresh_postgres.replace("postgresql+psycopg2://", "postgresql://")
        assert collect_rag_health(url).corpora == ()

    def test_the_connection_is_opened_read_only(self, store_url, monkeypatch):
        """A health check must not be able to write to, or migrate, what it inspects."""
        import psycopg2

        calls = []
        real_connect = psycopg2.connect

        def spy(*args, **kwargs):
            conn = real_connect(*args, **kwargs)
            real_set_session = conn.set_session

            class _Spy:
                def __getattr__(self, name):
                    return getattr(conn, name)

                def set_session(self, **kw):
                    calls.append(kw)
                    return real_set_session(**kw)

            return _Spy()

        # Scoped to the call under test: the fresh_postgres teardown connects too, and runs
        # before the monkeypatch fixture is undone.
        with monkeypatch.context() as scoped:
            scoped.setattr(psycopg2, "connect", spy)
            collect_rag_health(store_url)
        assert calls and calls[0].get("readonly") is True

    def test_a_write_through_that_connection_is_refused(self, store_url):
        """The same setting, proven by its effect: a read-only session cannot insert."""
        import psycopg2

        conn = psycopg2.connect(store_url)
        conn.set_session(readonly=True, autocommit=True)
        with conn.cursor() as cur, pytest.raises(psycopg2.errors.ReadOnlySqlTransaction):
            cur.execute("INSERT INTO documents (source_path) VALUES ('x')")
        conn.close()


# ---------------------------------------------------------------------------
# render_rag_health output
# ---------------------------------------------------------------------------


class TestRenderRagHealth:
    def test_render_empty(self, capsys):
        render_rag_health(RagHealth(corpora=(), total_chunks=0, healthy=False))
        out = capsys.readouterr().out
        assert "RAG" in out
        assert_rendered_text_names_no_domain(out, "the RAG health panel")

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
        monkeypatch.setattr("axiom.setup.wizard.collect_rag_health", lambda *a, **kw: sentinel)
        wizard.show_status()
        out = capsys.readouterr().out
        assert "Configuration Status" in out
        assert "RAG" in out
        assert "rag-community" in out
        assert "42" in out
