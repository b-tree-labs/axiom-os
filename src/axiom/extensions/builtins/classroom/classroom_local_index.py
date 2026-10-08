# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Per-classroom search index and document graph, on Postgres (ADR-174).

Phase 4 of the materials-flow tier. A classroom's index holds:

    - chunks, searched with Postgres full-text search (keyword, with prefix matching)
    - graph entities + edges (structural context; entities extracted by
      :mod:`axiom.graph.extractors.deterministic`)

It lives in the ``classroom`` schema (ADR-052). ``base_dir`` names the classroom's location, as
it always did, and now also names its scope: the index of one classroom never sees another's rows.
Semantic (vector) search is not part of this index: the old SQLite version created a vector table
and wrote embeddings that no query ever read, and every caller passed no embedder, so it was
removed rather than ported. Reuse of the platform retrieval store stays deferred.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import delete, func, select, text

from axiom.extensions.builtins.classroom.index_models import IndexChunk, IndexEdge, IndexEntity
from axiom.graph.extractors.deterministic import extract_from_document
from axiom.infra.schema_seam import SchemaSeam
from axiom.memory.stores import scope_for

#: The session seam for the ``classroom`` schema. Tests that cannot reach Postgres cannot run this
#: index: its search is Postgres full-text, and a stand-in would test something else.
seam = SchemaSeam("classroom", what="the classroom search index")

_STOPWORDS = frozenset(
    {
        "the", "a", "an", "and", "or", "of", "to", "is", "are",
        "what", "who", "where", "why", "how", "when", "this", "that",
        "these", "those", "does", "do", "did", "in", "on", "for",
        "with", "by", "from", "as", "at", "be", "can",
    }
)  # fmt: skip


# ---------------------------------------------------------------------------
# Results
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SearchHit:
    file_id: str
    title: str
    text: str
    score: float  # ts_rank: higher is more relevant


# ---------------------------------------------------------------------------
# Index
# ---------------------------------------------------------------------------


@dataclass
class ClassroomLocalIndex:
    base_dir: Path

    @property
    def scope(self) -> str:
        return scope_for("classroom-index", self.base_dir)

    # ---- Lifecycle ----

    def open(self) -> None:
        self.base_dir.mkdir(parents=True, exist_ok=True)
        seam.ensure_provisioned()

    def close(self) -> None:
        """Nothing to release: each call takes a session and returns it."""

    def has_content(self) -> bool:
        """True when this classroom has anything indexed (replaces "does index.db exist")."""
        with seam.session_scope() as s:
            return (
                s.execute(
                    select(IndexChunk.id).where(IndexChunk.scope == self.scope).limit(1)
                ).first()
                is not None
            )

    def drop(self) -> None:
        """Delete everything indexed for this classroom (a transient index must not outlive its run)."""
        scope = self.scope
        with seam.session_scope() as s:
            for model in (IndexChunk, IndexEntity, IndexEdge):
                s.execute(delete(model).where(model.scope == scope))
            s.commit()

    # ---- Ingest ----

    def ingest(self, *, file_id: str, title: str, content: str) -> None:
        """Ingest one document. Replaces prior ingestion for the same file_id.

        - Splits ``content`` into paragraph-ish chunks
        - Writes the chunks (full-text indexed by the database)
        - Extracts graph entities + edges from the full document text
        """
        chunks = _chunk_text(content, title=title)
        # Graph extraction (deterministic — no LLM).
        extracted = extract_from_document(text=content, source_path=file_id, source_type="markdown")
        scope = self.scope
        with seam.session_scope() as s:
            # Purge prior state for this file_id (re-ingest supersedes).
            for model in (IndexChunk, IndexEntity, IndexEdge):
                s.execute(delete(model).where(model.scope == scope, model.file_id == file_id))
            s.add_all(
                IndexChunk(scope=scope, file_id=file_id, title=title, chunk_index=i, text=chunk)
                for i, chunk in enumerate(chunks)
            )
            s.add_all(
                IndexEntity(
                    scope=scope,
                    file_id=file_id,
                    label=ent.label,
                    name=ent.name,
                    properties=ent.properties or {},
                    confidence=float(ent.confidence),
                )
                for ent in extracted.entities
            )
            s.add_all(
                IndexEdge(
                    scope=scope,
                    file_id=file_id,
                    rel_type=edge.rel_type,
                    from_name=edge.from_name,
                    from_label=edge.from_label,
                    to_name=edge.to_name,
                    to_label=edge.to_label,
                    confidence=float(edge.confidence),
                )
                for edge in extracted.edges
            )
            s.commit()

    # ---- Search ----

    def search(self, query: str, k: int = 5) -> list[SearchHit]:
        if not query.strip():
            return []
        # Strip non-alphanumerics, drop very short and stop-like tokens, then OR the remaining
        # prefixes together. OR (not AND) so a question like "what is a control rod?" still hits a
        # passage containing "control rods" although "what" / "is" / "a" do not appear; the rank
        # pushes the more relevant chunks to the top. Tokens are reduced to [a-z0-9_], so the
        # tsquery text cannot carry operators or quotes.
        raw_tokens = [
            "".join(ch for ch in tok if (ch.isascii() and ch.isalnum()) or ch == "_")
            for tok in query.lower().split()
        ]
        tokens = [t for t in raw_tokens if len(t) >= 2 and t not in _STOPWORDS]
        if not tokens:
            return []
        tsquery = " | ".join(f"{t}:*" for t in tokens)
        tsv = func.to_tsvector(text("'english'"), IndexChunk.text)
        q = func.to_tsquery(text("'english'"), tsquery)
        stmt = (
            select(IndexChunk.file_id, IndexChunk.title, IndexChunk.text, func.ts_rank(tsv, q))
            .where(IndexChunk.scope == self.scope, tsv.op("@@")(q))
            .order_by(func.ts_rank(tsv, q).desc(), IndexChunk.id.asc())
            .limit(k)
        )
        with seam.session_scope() as s:
            return [
                SearchHit(file_id=f, title=ti, text=tx, score=float(sc or 0.0))
                for f, ti, tx, sc in s.execute(stmt)
            ]

    # ---- Introspection ----

    def chunk_count(self) -> int:
        with seam.session_scope() as s:
            return int(
                s.execute(
                    select(func.count())
                    .select_from(IndexChunk)
                    .where(IndexChunk.scope == self.scope)
                ).scalar_one()
            )

    def entities(self, *, file_id: str | None = None) -> list[dict]:
        stmt = select(IndexEntity).where(IndexEntity.scope == self.scope).order_by(IndexEntity.id)
        if file_id is not None:
            stmt = stmt.where(IndexEntity.file_id == file_id)
        with seam.session_scope() as s:
            return [
                {
                    "file_id": r.file_id,
                    "label": r.label,
                    "name": r.name,
                    "properties": r.properties or {},
                    "confidence": r.confidence,
                }
                for r in s.execute(stmt).scalars()
            ]

    def edges(self, *, file_id: str | None = None) -> list[dict]:
        stmt = select(IndexEdge).where(IndexEdge.scope == self.scope).order_by(IndexEdge.id)
        if file_id is not None:
            stmt = stmt.where(IndexEdge.file_id == file_id)
        with seam.session_scope() as s:
            return [
                {
                    "file_id": r.file_id,
                    "rel_type": r.rel_type,
                    "from_name": r.from_name,
                    "from_label": r.from_label,
                    "to_name": r.to_name,
                    "to_label": r.to_label,
                    "confidence": r.confidence,
                }
                for r in s.execute(stmt).scalars()
            ]

    def neighbors_of_file(self, file_id: str) -> list[dict]:
        """Return entity dicts linked to ``file_id`` via any edge.

        Useful for Phase 6's Q&A to show "this document also mentions
        NUREG-1234 and 10 CFR 50.2" alongside retrieved chunks.
        """
        return self.entities(file_id=file_id)


# ---------------------------------------------------------------------------
# Chunking
# ---------------------------------------------------------------------------


def _chunk_text(text: str, title: str = "") -> list[str]:
    """Split ``text`` into chunks for indexing using graph-informed semantic chunking.

    Pipeline:
      1. Run the deterministic graph extractor over the full document to
         get structural boundaries enriched by entity/edge signal
         (cross-references, regulatory section markers, persons, headings).
      2. Pass those boundaries to ``axiom.rag.semantic_chunker.chunk_semantic``,
         which respects them when deciding split points.

    Falls back to plain semantic chunking if extraction fails for any reason.

    Day 1 lineage:
      - round 1: 400-char fixed-window shredder (rw-01 missed the answer)
      - round 2: plain semantic chunker (rw-01 surfaced 425°C)
      - round 3: graph-informed semantic chunker (this) — boundaries enriched
        by graph extraction; preserves entity-spanning context across short
        docs and respects regulatory/document references.
    """
    if not text or not text.strip():
        return []

    import os

    from axiom.rag.semantic_chunker import chunk_semantic

    # Escape hatch for benchmarking: AXIOM_CHUNKER_USE_GRAPH=0 disables
    # graph-extractor boundary augmentation, leaving plain semantic chunking.
    # Default (unset or any non-"0" value) → graph-informed.
    use_graph = os.environ.get("AXIOM_CHUNKER_USE_GRAPH", "1") != "0"

    boundaries = None
    if use_graph:
        try:
            from axiom.graph.extractors.deterministic import extract_from_document

            result = extract_from_document(text, title or "doc", "markdown")
            boundaries = result.boundaries
        except Exception:  # noqa: BLE001
            # Extraction must never bring ingestion down — fall back to plain semantic.
            boundaries = None

    chunks = chunk_semantic(text, path=title or "doc", boundaries=boundaries)
    return [c.text for c in chunks]


__all__ = [
    "ClassroomLocalIndex",
    "SearchHit",
]
