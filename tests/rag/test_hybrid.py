# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``axiom.rag.hybrid`` -- the ONE shared retrieval module (Neut chat P2c).

spec-harness-agnostic-site-access P1 ("One retrieval, served and logged") and
spec-unified-mcp-surface amendment #1 ("one shared retrieval module ... not a
third implementation") require chat and ``/api/v1/rag/search`` to return
identical chunk sets by construction and to log to one ``retrieval_log``.

No database: the fusion + rerank stages are pure, and the SQL ``retrieve``
emits is captured by a small recording connection/cursor stub that returns
canned rows. Rows follow the backend row shape
``(source_path, source_title, chunk_text, chunk_index, corpus, score)``.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import os
import subprocess
import sys
from pathlib import Path

import pytest

from axiom.rag.hybrid import (
    RETRIEVAL_LOG_DDL,
    Hit,
    HybridConfig,
    RerankWeights,
    RetrievalRecord,
    bind_log_sink,
    ensure_retrieval_log,
    make_retrieval_log_sink,
    retrieval_log_ddl,
    retrieve,
)

# ---------------------------------------------------------------------------
# Recording DB-API stub
# ---------------------------------------------------------------------------


class FakeCursor:
    def __init__(self, conn: FakeConn):
        self.conn = conn
        self._rows: list = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        self.conn.executed.append((sql, params))
        self._rows = self.conn.dispatch(sql, params)

    def fetchall(self):
        return list(self._rows)


class FakeConn:
    """Routes each statement by shape: SET / dense (<=>) / text (ts_rank) / INSERT / DDL."""

    def __init__(
        self,
        *,
        dense=(),
        text=(),
        entity=(),
        fail_text: bool = False,
        fail_insert: bool = False,
    ):
        self.dense = list(dense)
        self.text = list(text)
        self.entity = list(entity)
        self.fail_text = fail_text
        self.fail_insert = fail_insert
        self.executed: list[tuple[str, object]] = []
        self.rollbacks = 0
        self.commits = 0

    def cursor(self):
        return FakeCursor(self)

    def rollback(self):
        self.rollbacks += 1

    def commit(self):
        self.commits += 1

    def dispatch(self, sql: str, params):
        head = sql.strip().upper()
        if head.startswith("SET "):
            return []
        if head.startswith("INSERT"):
            if self.fail_insert:
                raise RuntimeError("permission denied for table retrieval_log")
            return []
        if head.startswith("CREATE"):
            return []
        if "<=>" in sql:
            return self.dense
        if "ts_rank" in sql:
            if self.fail_text:
                raise RuntimeError("canceling statement due to statement timeout")
            return self.entity if "&" in (params[0] or "") else self.text
        raise AssertionError(f"unexpected statement: {sql[:80]}")

    # helpers ---------------------------------------------------------------
    def statements(self, needle: str) -> list[tuple[str, object]]:
        return [(s, p) for s, p in self.executed if needle in s]


def row(path, title, text, idx=0, corpus="rag-community", score=0.5):
    return (path, title, text, idx, corpus, score)


CFG = HybridConfig(corpora=("rag-community", "rag-org"))
EMB = [0.1, 0.2, 0.3]


# ---------------------------------------------------------------------------
# HybridConfig
# ---------------------------------------------------------------------------


class TestHybridConfig:
    def test_defaults_match_the_vendored_constants(self):
        assert CFG.k == 8
        assert CFG.breadth == 40
        assert CFG.fusion_k == 60
        assert CFG.min_score is None
        assert CFG.access_tiers == ("public",)
        assert CFG.statement_timeout_ms == 2500
        assert CFG.ivfflat_probes is None
        assert CFG.mode == "hybrid"
        assert CFG.text_scan_cap == 4000
        assert CFG.rerank_weights == RerankWeights(
            coverage=3.0, value_intent=1.5, corpus_authority=1.0, rrf=1.0
        )

    @pytest.mark.parametrize(
        "kwargs",
        [
            {"k": 0},
            {"k": -1},
            {"k": 10, "breadth": 5},
            {"mode": "sparse"},
            {"fusion_k": 0},
            {"statement_timeout_ms": -1},
            {"ivfflat_probes": 0},
            {"text_scan_cap": 0},
            {"authority_path_re": "("},
            {"extra_code_re": "["},
        ],
    )
    def test_rejects_invalid_values(self, kwargs):
        with pytest.raises(ValueError):
            HybridConfig(corpora=("rag-org",), **kwargs)

    def test_requires_at_least_one_corpus(self):
        with pytest.raises(ValueError):
            HybridConfig(corpora=())

    def test_is_frozen_and_patterns_compile_once(self):
        cfg = HybridConfig(corpora=("rag-org",), authority_path_re=r"Ops|Manual")
        with pytest.raises(dataclasses.FrozenInstanceError):
            cfg.k = 3  # type: ignore[misc]
        assert cfg.authority_pattern is cfg.authority_pattern
        assert cfg.authority_pattern.search("site/Ops/handbook.pdf")
        assert CFG.authority_pattern is None
        assert CFG.extra_code_pattern is None


# ---------------------------------------------------------------------------
# Pure stages: fusion, rerank, floor, authority, dedup, cap
# ---------------------------------------------------------------------------


class TestFusionAndRerank:
    def test_chunk_in_both_rankings_outranks_single_ranking_chunks(self):
        # RRF: a chunk in two rankings scores 1/(60+r1) + 1/(60+r2); a chunk at
        # rank 1 in one ranking only scores 1/61. Rerank is off, so RRF order holds.
        both = row("p/both", "Both", "alpha text", corpus="rag-community")
        dense_only = row("p/dense", "Dense", "beta text")
        text_only = row("p/text", "Text", "gamma text")
        cfg = HybridConfig(corpora=("rag-community",), rerank=False)
        conn = FakeConn(dense=[dense_only, both], text=[text_only, both])
        hits = retrieve(conn, "alpha beta gamma", EMB, cfg)
        assert [h.source_path for h in hits] == ["p/both", "p/dense", "p/text"]
        assert hits[0].rank == 1 and hits[2].rank == 3
        assert hits[0].combined_score == pytest.approx(1 / 62 + 1 / 62)
        assert hits[1].combined_score == pytest.approx(1 / 61)

    def test_rerank_coverage_beats_rrf_position(self):
        # Dense puts the generic chunk first; the chunk carrying every query
        # term wins on coverage (weight 3.0) despite a lower RRF score.
        generic = row("p/generic", "Generic", "an overview of many unrelated things")
        exact = row("p/exact", "Exact", "the pump seal pressure rating is listed here")
        conn = FakeConn(dense=[generic, exact])
        hits = retrieve(conn, "pump seal pressure", EMB, CFG)
        assert hits[0].source_path == "p/exact"
        assert hits[0].combined_score > hits[1].combined_score

    def test_rerank_off_or_empty_query_keeps_rrf_order(self):
        generic = row("p/generic", "Generic", "an overview of many unrelated things")
        exact = row("p/exact", "Exact", "the pump seal pressure rating is listed here")
        off = HybridConfig(corpora=("rag-community",), rerank=False)
        conn = FakeConn(dense=[generic, exact])
        assert [h.source_path for h in retrieve(conn, "pump seal pressure", EMB, off)] == [
            "p/generic",
            "p/exact",
        ]
        # An all-stopword query has no terms to cover: RRF order, no crash.
        conn = FakeConn(dense=[generic, exact])
        assert [h.source_path for h in retrieve(conn, "the of", EMB, CFG)] == [
            "p/generic",
            "p/exact",
        ]

    def test_value_intent_boosts_chunk_carrying_a_value(self):
        prose = row("p/prose", "Prose", "the pump rating is discussed at length in prose")
        valued = row("p/value", "Value", "the pump rating measured -1.16 in the test")
        conn = FakeConn(dense=[prose, valued])
        hits = retrieve(conn, "pump rating worth", EMB, CFG)
        assert hits[0].source_path == "p/value"

    def test_min_score_floor_drops_low_dense_hits(self):
        strong = row("p/strong", "S", "pump seal text", score=0.91)
        weak = row("p/weak", "W", "pump seal other text", score=0.31)
        cfg = HybridConfig(corpora=("rag-community",), min_score=0.5)
        hits = retrieve(FakeConn(dense=[strong, weak]), "pump seal", EMB, cfg)
        assert [h.source_path for h in hits] == ["p/strong"]
        assert hits[0].similarity == pytest.approx(0.91)
        # No floor: both survive.
        hits = retrieve(FakeConn(dense=[strong, weak]), "pump seal", EMB, CFG)
        assert {h.source_path for h in hits} == {"p/strong", "p/weak"}

    def test_min_score_applies_to_dense_ranking_only(self):
        # A text-only candidate has no dense similarity and is never floored.
        weak = row("p/weak", "W", "pump seal text", score=0.2)
        textual = row("p/text", "T", "pump seal other text", score=0.05)
        cfg = HybridConfig(corpora=("rag-community",), min_score=0.5)
        hits = retrieve(FakeConn(dense=[weak], text=[textual]), "pump seal", EMB, cfg)
        assert [h.source_path for h in hits] == ["p/text"]
        assert hits[0].similarity == 0.0

    def test_authority_corpus_boosts_ordering(self):
        community = row("p/c", "C", "pump seal pressure note", corpus="rag-community")
        org = row("p/o", "O", "pump seal pressure memo", corpus="rag-org")
        base = HybridConfig(corpora=("rag-community", "rag-org"))
        assert (
            retrieve(FakeConn(dense=[community, org]), "pump seal pressure", EMB, base)[
                0
            ].source_path
            == "p/c"
        )
        boosted = HybridConfig(corpora=("rag-community", "rag-org"), authority_corpus="rag-org")
        assert (
            retrieve(FakeConn(dense=[community, org]), "pump seal pressure", EMB, boosted)[
                0
            ].source_path
            == "p/o"
        )

    def test_authority_path_re_boosts_ordering_by_path_or_title(self):
        generic = row("lit/paper.pdf", "A paper", "pump seal pressure note")
        by_path = row("site/Operations/manual.pdf", "Manual", "pump seal pressure memo")
        by_title = row("lit/other.pdf", "Tech Spec 4", "pump seal pressure spec")
        cfg = HybridConfig(corpora=("rag-community",), authority_path_re=r"Operations|Tech Spec")
        hits = retrieve(
            FakeConn(dense=[generic, by_path, by_title]), "pump seal pressure", EMB, cfg
        )
        assert [h.source_path for h in hits] == [
            "site/Operations/manual.pdf",
            "lit/other.pdf",
            "lit/paper.pdf",
        ]

    def test_rerank_weights_are_honoured(self):
        community = row("p/c", "C", "pump seal pressure note", corpus="rag-community")
        org = row("p/o", "O", "pump seal pressure memo", corpus="rag-org")
        cfg = HybridConfig(
            corpora=("rag-community", "rag-org"),
            authority_corpus="rag-org",
            rerank_weights=RerankWeights(corpus_authority=0.0),
        )
        assert (
            retrieve(FakeConn(dense=[community, org]), "pump seal pressure", EMB, cfg)[
                0
            ].source_path
            == "p/c"
        )

    def test_dedup_by_text_prefix(self):
        body = "x" * 120
        a = row("p/a", "A", body + " tail one", idx=0)
        b = row("p/b", "B", body + " tail two", idx=1)
        c = row("p/c", "C", "different body", idx=2)
        hits = retrieve(FakeConn(dense=[a, b, c]), "the of", EMB, CFG)
        assert [h.source_path for h in hits] == ["p/a", "p/c"]

    def test_k_caps_results_and_ranks_are_dense(self):
        rows = [row(f"p/{i}", f"T{i}", f"chunk body number {i}", idx=i) for i in range(40)]
        cfg = HybridConfig(corpora=("rag-community",), k=5, breadth=40)
        hits = retrieve(FakeConn(dense=rows), "chunk body", EMB, cfg)
        assert len(hits) == 5
        assert [h.rank for h in hits] == [1, 2, 3, 4, 5]

    def test_chunk_identity_is_path_index_corpus(self):
        # Same path + index in two corpora are distinct chunks; same key across
        # rankings is one chunk.
        a = row("p/a", "A", "first body", idx=0, corpus="rag-org")
        a_other = row("p/a", "A", "second body", idx=0, corpus="rag-community")
        conn = FakeConn(dense=[a], text=[a, a_other])
        cfg = HybridConfig(corpora=("rag-org", "rag-community"), rerank=False)
        hits = retrieve(conn, "body", EMB, cfg)
        assert [(h.source_path, h.corpus) for h in hits] == [
            ("p/a", "rag-org"),
            ("p/a", "rag-community"),
        ]


# ---------------------------------------------------------------------------
# Hit / RetrievalRecord shapes
# ---------------------------------------------------------------------------


class TestShapes:
    def test_hit_to_dict_shape(self):
        hit = Hit(
            source_path="p/a",
            source_title="A",
            chunk_text="body",
            chunk_index=3,
            corpus="rag-org",
            similarity=0.8,
            combined_score=4.25,
            rank=1,
        )
        d = hit.to_dict()
        assert d == {
            "source_path": "p/a",
            "source_title": "A",
            "chunk_text": "body",
            "chunk_index": 3,
            "corpus": "rag-org",
            "similarity": 0.8,
            "combined_score": 4.25,
            "rank": 1,
        }
        json.dumps(d)

    def test_log_entry_carries_identity_and_score_but_no_chunk_body(self):
        hit = Hit("p/a", "A", "secret body", 3, "rag-org", 0.8, 4.25, 1)
        entry = hit.to_log_entry()
        assert entry == {
            "title": "A",
            "path": "p/a",
            "chunk_index": 3,
            "corpus": "rag-org",
            "score": 4.25,
        }
        assert "secret body" not in json.dumps(entry)

    def test_hits_from_retrieve_carry_similarity_for_dense_chunks(self):
        dense = row("p/d", "D", "pump seal", score=0.77)
        hits = retrieve(FakeConn(dense=[dense]), "pump seal", EMB, CFG)
        assert hits[0].similarity == pytest.approx(0.77)
        assert hits[0].chunk_index == 0 and hits[0].corpus == "rag-community"


# ---------------------------------------------------------------------------
# SQL level: what retrieve() sends to the connection
# ---------------------------------------------------------------------------


class TestRetrieveSQL:
    def test_dense_query_carries_embedding_corpora_tiers_and_breadth(self):
        conn = FakeConn(dense=[row("p/a", "A", "pump seal")])
        cfg = HybridConfig(
            corpora=("rag-org", "rag-community"), access_tiers=("public", "course"), breadth=25
        )
        retrieve(conn, "pump seal", EMB, cfg)
        ((sql, params),) = conn.statements("<=>")
        assert "FROM chunks" in sql
        assert "embedding IS NOT NULL" in sql
        assert "corpus = ANY(%s)" in sql and "access_tier = ANY(%s)" in sql
        assert params == (
            "[0.100000,0.200000,0.300000]",
            ["rag-org", "rag-community"],
            ["public", "course"],
            "[0.100000,0.200000,0.300000]",
            25,
        )

    def test_text_queries_carry_tsquery_filters_scan_cap_and_breadth(self):
        conn = FakeConn(dense=[], text=[], entity=[])
        cfg = HybridConfig(corpora=("rag-org",), text_scan_cap=1234, breadth=33)
        retrieve(conn, "pump seal pressure on T3", EMB, cfg)
        text_stmts = conn.statements("ts_rank")
        assert len(text_stmts) == 2, "broad OR ranking + entity-required ranking"
        (sql, params), (_esql, eparams) = text_stmts
        assert "to_tsquery('english', %s)" in sql
        assert "corpus = ANY(%s)" in sql and "access_tier = ANY(%s)" in sql
        assert params == (
            "t3 | pump | seal | pressure",
            ["rag-org"],
            ["public"],
            "t3 | pump | seal | pressure",
            1234,
            33,
        )
        assert eparams[0] == "(t3) & (pump | seal | pressure)"
        assert eparams[0] == eparams[3]

    def test_entity_ranking_skipped_when_it_equals_the_broad_query(self):
        conn = FakeConn()
        retrieve(conn, "T3", EMB, CFG)
        assert len(conn.statements("ts_rank")) == 1

    def test_text_backend_skipped_for_all_stopword_query(self):
        conn = FakeConn(dense=[row("p/a", "A", "body")])
        retrieve(conn, "the of a", EMB, CFG)
        assert conn.statements("ts_rank") == []

    def test_session_settings_timeout_and_optional_probes(self):
        conn = FakeConn()
        retrieve(conn, "pump", EMB, HybridConfig(corpora=("rag-org",), statement_timeout_ms=750))
        sets = [s for s, _ in conn.executed if s.upper().startswith("SET ")]
        assert sets == ["SET statement_timeout = 750"]
        conn = FakeConn()
        retrieve(conn, "pump", EMB, HybridConfig(corpora=("rag-org",), ivfflat_probes=10))
        sets = [s for s, _ in conn.executed if s.upper().startswith("SET ")]
        assert sets == ["SET statement_timeout = 2500", "SET ivfflat.probes = 10"]
        # Settings are applied before any candidate query.
        assert conn.executed[0][0].startswith("SET ")

    def test_empty_access_tiers_means_no_tier_filter(self):
        conn = FakeConn()
        retrieve(conn, "pump", EMB, HybridConfig(corpora=("rag-org",), access_tiers=()))
        for sql, params in conn.statements("FROM chunks"):
            assert "access_tier" not in sql
            assert ["public"] not in params

    def test_mode_dense_runs_only_the_dense_query(self):
        conn = FakeConn(dense=[row("p/a", "A", "pump seal")])
        hits = retrieve(conn, "pump seal", EMB, HybridConfig(corpora=("rag-org",), mode="dense"))
        assert len(conn.statements("<=>")) == 1
        assert conn.statements("ts_rank") == []
        assert [h.source_path for h in hits] == ["p/a"]

    def test_mode_text_runs_no_dense_query_even_with_an_embedding(self):
        conn = FakeConn(text=[row("p/t", "T", "pump seal")])
        hits = retrieve(conn, "pump seal", EMB, HybridConfig(corpora=("rag-org",), mode="text"))
        assert conn.statements("<=>") == []
        assert len(conn.statements("ts_rank")) == 1
        assert [h.source_path for h in hits] == ["p/t"]

    def test_hybrid_without_embedding_falls_back_to_text_with_a_note(self):
        conn = FakeConn(text=[row("p/t", "T", "pump seal")])
        seen: list[RetrievalRecord] = []
        hits = retrieve(conn, "pump seal", None, CFG, log_sink=seen.append)
        assert conn.statements("<=>") == []
        assert [h.source_path for h in hits] == ["p/t"]
        assert "dense" in (seen[0].note or "")

    def test_dense_mode_without_embedding_returns_nothing_and_notes_it(self):
        conn = FakeConn(dense=[row("p/a", "A", "pump")])
        seen: list[RetrievalRecord] = []
        cfg = HybridConfig(corpora=("rag-org",), mode="dense")
        assert retrieve(conn, "pump", None, cfg, log_sink=seen.append) == []
        assert conn.statements("FROM chunks") == []
        assert seen[0].result_count == 0 and seen[0].note

    def test_text_backend_failure_is_fail_soft_with_rollback_and_note(self):
        dense = row("p/d", "D", "pump seal pressure")
        conn = FakeConn(dense=[dense], fail_text=True)
        seen: list[RetrievalRecord] = []
        hits = retrieve(conn, "pump seal pressure on T3", EMB, CFG, log_sink=seen.append)
        assert [h.source_path for h in hits] == ["p/d"]
        assert conn.rollbacks == 2, "each failed text ranking rolls back its aborted statement"
        assert "text" in seen[0].note and "entity" in seen[0].note
        # Session settings are re-applied after a rollback reverted them.
        sets = [s for s, _ in conn.executed if s.upper().startswith("SET ")]
        assert len(sets) == 3

    def test_extra_code_re_adds_site_codes_to_the_text_query(self):
        conn = FakeConn()
        cfg = HybridConfig(corpora=("rag-org",), extra_code_re=r"n-ms")
        retrieve(conn, "worth of the n-ms facility", EMB, cfg)
        (_sql, params), *_ = conn.statements("ts_rank")
        assert params[0] == "nms | worth | facility"
        conn = FakeConn()
        retrieve(conn, "worth of the n-ms facility", EMB, CFG)
        (_sql, params), *_ = conn.statements("ts_rank")
        assert params[0] == "worth | facility"

    def test_empty_query_and_no_embedding_runs_nothing(self):
        conn = FakeConn()
        assert retrieve(conn, "", None, CFG) == []
        assert conn.statements("FROM chunks") == []


# ---------------------------------------------------------------------------
# Log sink plumbing
# ---------------------------------------------------------------------------


class TestLogSink:
    def test_log_sink_called_once_with_count_latency_and_coarse_results(self):
        conn = FakeConn(dense=[row("p/a", "A", "pump seal body", idx=2, corpus="rag-org")])
        seen: list[RetrievalRecord] = []
        hits = retrieve(conn, "pump seal", EMB, CFG, log_sink=seen.append)
        assert len(seen) == 1
        rec = seen[0]
        assert rec.query_text == "pump seal"
        assert rec.mode == "hybrid" and rec.k == 8
        assert rec.result_count == len(hits) == 1
        assert rec.latency_ms >= 0.0
        assert rec.note is None
        assert rec.principal is None and rec.source == "rag_search"
        assert list(rec.results) == [
            {
                "title": "A",
                "path": "p/a",
                "chunk_index": 2,
                "corpus": "rag-org",
                "score": hits[0].combined_score,
            }
        ]
        assert "pump seal body" not in json.dumps(list(rec.results))

    def test_zero_hit_turn_is_logged(self):
        seen: list[RetrievalRecord] = []
        retrieve(FakeConn(), "pump seal", EMB, CFG, log_sink=seen.append)
        assert seen[0].result_count == 0 and list(seen[0].results) == []

    def test_bind_log_sink_stamps_principal_and_source(self):
        seen: list[RetrievalRecord] = []
        bound = bind_log_sink(seen.append, principal="node:peer-1", source="chat")
        retrieve(FakeConn(), "pump", EMB, CFG, log_sink=bound)
        assert seen[0].principal == "node:peer-1" and seen[0].source == "chat"

    def test_raising_log_sink_never_breaks_retrieval(self, caplog):
        def boom(_rec):
            raise RuntimeError("sink down")

        conn = FakeConn(dense=[row("p/a", "A", "pump seal")])
        with caplog.at_level(logging.WARNING, logger="axiom.rag.hybrid"):
            hits = retrieve(conn, "pump seal", EMB, CFG, log_sink=boom)
        assert [h.source_path for h in hits] == ["p/a"]
        assert any("sink down" in r.getMessage() for r in caplog.records)

    def test_make_retrieval_log_sink_inserts_expected_columns_and_commits(self):
        conn = FakeConn()
        sink = make_retrieval_log_sink(conn)
        rec = RetrievalRecord(
            query_text="pump seal",
            mode="hybrid",
            k=8,
            result_count=1,
            latency_ms=12.5,
            results=(
                {"title": "A", "path": "p/a", "chunk_index": 0, "corpus": "rag-org", "score": 1.5},
            ),
            note=None,
            principal="rag-search",
            source="rag_search",
        )
        sink(rec)
        ((sql, params),) = conn.statements("INSERT")
        assert sql.startswith("INSERT INTO retrieval_log ")
        assert (
            "(principal, source, query_text, mode, k, result_count, results, latency_ms, note)"
            in sql
        )
        assert "%s::jsonb" in sql
        assert params[:6] == ("rag-search", "rag_search", "pump seal", "hybrid", 8, 1)
        assert json.loads(params[6]) == list(rec.results)
        assert params[7:] == (12.5, None)
        assert conn.commits == 1

    def test_make_retrieval_log_sink_accepts_a_qualified_table(self):
        conn = FakeConn()
        sink = make_retrieval_log_sink(conn, table="public.retrieval_log")
        sink(RetrievalRecord("q", "hybrid", 8, 0, 1.0, ()))
        ((sql, _),) = conn.statements("INSERT")
        assert sql.startswith("INSERT INTO public.retrieval_log ")
        with pytest.raises(ValueError):
            make_retrieval_log_sink(conn, table="retrieval_log; DROP TABLE chunks")

    def test_make_retrieval_log_sink_swallows_a_raising_cursor(self, caplog):
        conn = FakeConn(fail_insert=True)
        sink = make_retrieval_log_sink(conn)
        with caplog.at_level(logging.WARNING, logger="axiom.rag.hybrid"):
            sink(RetrievalRecord("q", "hybrid", 8, 0, 1.0, ()))  # must not raise
        assert conn.rollbacks == 1
        assert any("retrieval_log" in r.getMessage() for r in caplog.records)

    def test_ddl_matches_the_audit_log_shape(self):
        for col in (
            "id            bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY",
            "ts            timestamptz NOT NULL DEFAULT now()",
            "principal     text",
            "source        text NOT NULL DEFAULT 'rag_search'",
            "query_text    text NOT NULL",
            "mode          text NOT NULL DEFAULT 'hybrid'",
            "k             int  NOT NULL",
            "result_count  int  NOT NULL",
            "results       jsonb NOT NULL DEFAULT '[]'::jsonb",
            "latency_ms    double precision",
            "note          text",
        ):
            assert col in RETRIEVAL_LOG_DDL
        assert "CREATE TABLE IF NOT EXISTS retrieval_log (" in RETRIEVAL_LOG_DDL
        assert "CREATE INDEX IF NOT EXISTS idx_retrieval_log_ts" in RETRIEVAL_LOG_DDL
        assert "idx_retrieval_log_zero_hits" in RETRIEVAL_LOG_DDL
        assert "WHERE result_count = 0" in RETRIEVAL_LOG_DDL
        # A schema-qualified name keeps the same index names, so a node that
        # already ran the site migration sees IF NOT EXISTS match.
        qualified = retrieval_log_ddl("public.retrieval_log")
        assert "CREATE TABLE IF NOT EXISTS public.retrieval_log (" in qualified
        assert "idx_retrieval_log_ts ON public.retrieval_log" in qualified
        assert "GRANT" not in qualified

    def test_ensure_retrieval_log_executes_ddl_and_commits(self):
        conn = FakeConn()
        ensure_retrieval_log(conn)
        ((sql, _),) = conn.statements("CREATE TABLE")
        assert sql == RETRIEVAL_LOG_DDL
        assert conn.commits == 1


# ---------------------------------------------------------------------------
# Importability: a serving venv with only a psycopg must import this module
# ---------------------------------------------------------------------------


def test_import_pulls_in_no_driver_sqlalchemy_or_platform_modules():
    src = Path(__file__).resolve().parents[2] / "src"
    code = (
        "import sys\n"
        "import axiom.rag.hybrid as h\n"
        "bad = sorted(m for m in sys.modules if m.split('.')[0] in "
        "('sqlalchemy', 'psycopg', 'psycopg2', 'pgvector', 'numpy'))\n"
        "assert not bad, bad\n"
        "assert 'axiom.rag.store' not in sys.modules\n"
        "assert 'axiom.rag.retriever' not in sys.modules\n"
        "assert not [m for m in sys.modules if m.startswith('axiom.federation')], 'federation'\n"
        "assert not [m for m in sys.modules if m.startswith('axiom.identity')], 'identity'\n"
        "print(h.__file__)\n"
    )
    env = {**os.environ, "PYTHONPATH": str(src), "PYTHONSAFEPATH": "1"}
    proc = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, env=env, timeout=60
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip().startswith(str(src)), proc.stdout
