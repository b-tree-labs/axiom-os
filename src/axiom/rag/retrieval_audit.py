# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Retrieval audit log — per-turn record of what the retriever surfaced.

Distinct from ``interaction_log`` (CURIO quality loop). Captures:
    - the query,
    - every retrieved chunk's stable ``citation_key`` + provenance,
    - which keys the model actually cited,
    - which markers were unresolved (potential hallucinated citations),
    - which retrieved keys went unused (prompt noise signal).

Reads from ``RetrievedChunk`` (retriever) + ``CitationEnvelope``
(postprocessor). Writes to Postgres via the RAG store's connection.
Failures are swallowed — the audit log must never break a chat turn.
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Iterable
from dataclasses import dataclass

from axiom.rag.citation import CitationEnvelope
from axiom.rag.retriever import RetrievedChunk

log = logging.getLogger(__name__)


RETRIEVAL_AUDIT_DDL = """\
CREATE TABLE IF NOT EXISTS retrieval_audit (
    id              BIGSERIAL PRIMARY KEY,
    session_id      TEXT NOT NULL DEFAULT '',
    principal_id    TEXT NOT NULL DEFAULT '',
    query_text      TEXT NOT NULL,
    query_hash      TEXT NOT NULL,
    retrieved_count INTEGER NOT NULL DEFAULT 0,
    retrieved_chunks TEXT NOT NULL DEFAULT '[]',   -- JSON array
    cited_keys      TEXT NOT NULL DEFAULT '',       -- comma-separated
    unresolved_keys TEXT NOT NULL DEFAULT '',       -- comma-separated
    unused_keys     TEXT NOT NULL DEFAULT '',       -- comma-separated
    latency_ms      INTEGER NOT NULL DEFAULT 0,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_retrieval_audit_session ON retrieval_audit (session_id);
CREATE INDEX IF NOT EXISTS idx_retrieval_audit_principal ON retrieval_audit (principal_id);
CREATE INDEX IF NOT EXISTS idx_retrieval_audit_query_hash ON retrieval_audit (query_hash);
"""


@dataclass(frozen=True)
class AuditRecord:
    """In-memory view of one retrieval-audit row."""

    query_text: str
    query_hash: str
    retrieved_count: int
    retrieved_chunks_json: str
    cited_keys: str
    unresolved_keys: str
    unused_keys: str
    session_id: str = ""
    principal_id: str = ""
    latency_ms: int = 0

    @classmethod
    def from_retrieval(
        cls,
        *,
        query_text: str,
        retrieved: Iterable[RetrievedChunk],
        envelope: CitationEnvelope | None,
        session_id: str = "",
        principal_id: str = "",
        latency_ms: int = 0,
    ) -> AuditRecord:
        retrieved_list = list(retrieved)
        payload = [
            {
                "citation_key": c.citation_key,
                "rank": c.rank,
                "source_path": c.source_path,
                "chunk_index": c.chunk_index,
                "corpus": c.corpus,
                "rrf_score": c.rrf_score,
                "similarity": c.similarity,
                "access_tier": c.access_tier,
                "classification": c.classification,
            }
            for c in retrieved_list
        ]
        if envelope is not None:
            cited = ",".join(c.citation_key for c in envelope.cited)
            unresolved = ",".join(envelope.unresolved)
            unused = ",".join(envelope.unused)
        else:
            cited = unresolved = unused = ""
        return cls(
            query_text=query_text,
            query_hash=_query_hash(query_text),
            retrieved_count=len(retrieved_list),
            retrieved_chunks_json=json.dumps(payload),
            cited_keys=cited,
            unresolved_keys=unresolved,
            unused_keys=unused,
            session_id=session_id,
            principal_id=principal_id,
            latency_ms=latency_ms,
        )


def _query_hash(text: str) -> str:
    return hashlib.sha256(text.strip().lower().encode("utf-8")).hexdigest()[:16]


def ensure_retrieval_audit(conn) -> None:
    """Create the retrieval_audit table if it doesn't exist."""
    if conn is None:
        return
    try:
        with conn.cursor() as cur:
            cur.execute(RETRIEVAL_AUDIT_DDL)
    except Exception as exc:  # pragma: no cover — schema setup is resilient
        log.warning("retrieval_audit DDL failed: %s", exc)


def log_retrieval_audit(
    store,
    *,
    query_text: str,
    retrieved: Iterable[RetrievedChunk],
    envelope: CitationEnvelope | None,
    session_id: str = "",
    principal_id: str = "",
    latency_ms: int = 0,
) -> None:
    """Record one retrieval event. Safe to call on every chat turn.

    Errors are swallowed — the audit log must never break the chat turn.
    """
    conn = getattr(store, "_conn", None)
    if conn is None:
        return
    record = AuditRecord.from_retrieval(
        query_text=query_text,
        retrieved=retrieved,
        envelope=envelope,
        session_id=session_id,
        principal_id=principal_id,
        latency_ms=latency_ms,
    )
    sql = (
        "INSERT INTO retrieval_audit "
        "(session_id, principal_id, query_text, query_hash, retrieved_count, "
        " retrieved_chunks, cited_keys, unresolved_keys, unused_keys, latency_ms) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)"
    )
    params = (
        record.session_id,
        record.principal_id,
        record.query_text,
        record.query_hash,
        record.retrieved_count,
        record.retrieved_chunks_json,
        record.cited_keys,
        record.unresolved_keys,
        record.unused_keys,
        record.latency_ms,
    )
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params)
    except Exception as exc:
        log.warning("retrieval_audit insert failed: %s", exc)
