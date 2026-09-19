# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""TDD tests for T0-1 retrieval audit log.

Each chat turn that triggers RAG records what was retrieved, what was
cited, and what the model failed to resolve. Used for:
    - Grounding metrics (cite-rate, unresolved-rate)
    - Post-hoc forensics ("why did the model cite that passage?")
    - Security review (did classified chunks surface to non-entitled users?)

Distinct from ``interaction_log`` (CURIO quality loop) so the two
concerns don't entangle.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest

from axiom.rag.citation import CitationEnvelope, CitationReference
from axiom.rag.retrieval_audit import AuditRecord, log_retrieval_audit
from axiom.rag.retriever import RetrievedChunk

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _ch(key: str, path: str) -> RetrievedChunk:
    return RetrievedChunk(
        citation_key=key,
        rank=int(key[1:]),
        source_path=path,
        source_title=path,
        chunk_text="...",
        chunk_index=0,
        corpus="rag-internal",
        similarity=0.6,
        rrf_score=0.03,
    )


def _fake_store() -> MagicMock:
    """Mock store with a capturing cursor; records executed SQL + params."""
    store = MagicMock()
    store._conn = MagicMock()
    cur = MagicMock()
    store._conn.cursor.return_value.__enter__.return_value = cur
    store._conn.cursor.return_value.__exit__.return_value = False
    store._cur = cur  # convenience alias
    return store


# ---------------------------------------------------------------------------
# Basic logging
# ---------------------------------------------------------------------------


class TestLogRetrievalAudit:
    def test_logs_query_and_retrieved_chunks(self):
        store = _fake_store()
        retrieved = [_ch("C1", "a.md"), _ch("C2", "b.md")]
        env = CitationEnvelope(
            text="[C1]",
            cited=[CitationReference(
                citation_key="C1", source_path="a.md", source_title="a.md",
                chunk_index=0, corpus="rag-internal", mention_count=1,
            )],
            unresolved=[],
            unused=["C2"],
        )
        log_retrieval_audit(
            store,
            query_text="quantum",
            retrieved=retrieved,
            envelope=env,
            session_id="sess-1",
            principal_id="@alice:ut",
            latency_ms=120,
        )
        # Cursor received an INSERT.
        store._cur.execute.assert_called_once()
        sql, params = store._cur.execute.call_args[0]
        assert "INSERT INTO retrieval_audit" in sql
        # Params: session_id, principal_id, query_text, query_hash,
        #         retrieved_count, retrieved_chunks(JSON),
        #         cited_keys, unresolved_keys, unused_keys, latency_ms
        assert params[0] == "sess-1"
        assert params[1] == "@alice:ut"
        assert params[2] == "quantum"
        assert params[4] == 2  # retrieved_count
        payload = json.loads(params[5])
        assert [r["citation_key"] for r in payload] == ["C1", "C2"]
        assert params[6] == "C1"           # cited_keys
        assert params[7] == ""             # unresolved_keys
        assert params[8] == "C2"           # unused_keys
        assert params[9] == 120            # latency_ms

    def test_logs_unresolved_markers(self):
        store = _fake_store()
        retrieved = [_ch("C1", "a.md")]
        env = CitationEnvelope(
            text="[C1] and [C9]",
            cited=[CitationReference(
                citation_key="C1", source_path="a.md", source_title="a.md",
                chunk_index=0, corpus="rag-internal", mention_count=1,
            )],
            unresolved=["C9"],
            unused=[],
        )
        log_retrieval_audit(
            store, query_text="q", retrieved=retrieved, envelope=env,
        )
        _, params = store._cur.execute.call_args[0]
        assert params[7] == "C9"  # unresolved_keys

    def test_retrieved_chunks_payload_shape(self):
        """The JSON payload must carry enough to reconstruct the retrieval."""
        store = _fake_store()
        retrieved = [
            RetrievedChunk(
                citation_key="C1", rank=1,
                source_path="a.md", source_title="Doc A",
                chunk_text="...", chunk_index=3, corpus="rag-org",
                similarity=0.77, rrf_score=0.031,
                access_tier="institutional", classification="sbu",
            )
        ]
        env = CitationEnvelope(text="", cited=[], unresolved=[], unused=["C1"])
        log_retrieval_audit(store, query_text="q", retrieved=retrieved, envelope=env)
        _, params = store._cur.execute.call_args[0]
        entry = json.loads(params[5])[0]
        assert entry["citation_key"] == "C1"
        assert entry["source_path"] == "a.md"
        assert entry["chunk_index"] == 3
        assert entry["corpus"] == "rag-org"
        assert entry["access_tier"] == "institutional"
        assert entry["classification"] == "sbu"
        assert entry["rrf_score"] == pytest.approx(0.031)


class TestResilience:
    def test_no_conn_is_silent_no_raise(self):
        """A store without a live connection must not raise."""
        store = MagicMock()
        store._conn = None
        # Must not raise.
        log_retrieval_audit(
            store, query_text="q", retrieved=[], envelope=None,
        )

    def test_cursor_failure_swallowed(self):
        """DB errors are logged and swallowed — never break the chat turn."""
        store = _fake_store()
        store._cur.execute.side_effect = RuntimeError("connection lost")
        # Must not raise.
        log_retrieval_audit(
            store, query_text="q",
            retrieved=[_ch("C1", "a.md")],
            envelope=None,
        )


class TestAuditRecord:
    def test_from_inputs_constructs_record(self):
        retrieved = [_ch("C1", "a.md")]
        env = CitationEnvelope(
            text="[C1]",
            cited=[CitationReference(
                citation_key="C1", source_path="a.md", source_title="a.md",
                chunk_index=0, corpus="rag-internal", mention_count=1,
            )],
            unresolved=[],
            unused=[],
        )
        rec = AuditRecord.from_retrieval(
            query_text="q", retrieved=retrieved, envelope=env,
            session_id="s", principal_id="@a:b", latency_ms=42,
        )
        assert rec.retrieved_count == 1
        assert rec.cited_keys == "C1"
        assert rec.latency_ms == 42
