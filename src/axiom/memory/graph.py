# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Layer 2 — Concept graph protocols + SQLite-backed default impl.

Per ``spec-memory.md §5`` and ADR-033 Layer 2. Provides:

- ``Concept`` and ``ConceptEdge`` dataclasses (the data model)
- ``ConceptGraph`` protocol (the contract every backend implements)
- ``ConceptExtractor`` protocol with ``ExtractorCapability`` for
  classification-aware extractor selection (spec-memory I11)
- ``SQLiteConceptGraph`` — Edge / Workstation default backend
- ``register_extractor`` / ``run_extractors_for_fragment`` —
  the wiring that lets ``CompositionService`` invoke extractors
  on append per the I11 contract

The protocol is the load-bearing contract; backends + extractors
are pluggable below it. Apache AGE on Postgres slots in as the
Server-tier backend (see spec-knowledge-graph §0); Cognee
components plug in selectively per ``working/cognee-vs-build-study``.
"""

from __future__ import annotations

import hashlib
import re
import sqlite3
import threading
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import (
    TYPE_CHECKING,
    Any,
    Protocol,
)

if TYPE_CHECKING:
    from axiom.memory.fragment import CognitiveType, MemoryFragment


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


def canonical_concept_id(canonical_name: str) -> str:
    """Deterministic concept id from canonical name.

    Same name → same id, across extensions, across nodes. This is what
    makes federated concept-level joins work without a global registry —
    nodes naturally agree on identity by hashing the same canonical
    string. Normalization: lowercase, whitespace collapsed.
    """
    normalized = " ".join(canonical_name.lower().split())
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]


@dataclass(frozen=True)
class Concept:
    """A node in the concept graph.

    ``concept_id`` is deterministic from ``canonical_name`` per the
    federation invariant. ``extracted_from`` is the provenance chain
    back to L1 fragments — every concept points at the events that
    produced it (spec-memory I10).
    """

    concept_id: str
    canonical_name: str
    extracted_from: list[str] = field(default_factory=list)
    confidence: float = 1.0


@dataclass(frozen=True)
class ConceptEdge:
    """A typed, weighted edge between concepts.

    ``evidence`` lists the fragment_ids supporting this edge, again
    enforcing provenance back to L1.
    """

    from_concept: str
    to_concept: str
    edge_type: str
    weight: float = 1.0
    evidence: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class GraphQuery:
    """Generic graph query. Backends may add specialized query types."""

    seed_concepts: frozenset[str] = frozenset()
    edge_types: frozenset[str] | None = None
    max_hops: int = 1
    min_weight: float = 0.0
    limit: int = 50


# ---------------------------------------------------------------------------
# Extractor capability — classification-aware extractor selection
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ExtractorCapability:
    """Per-extractor data-flow declaration.

    The registration layer matches ``max_classification`` against
    ``fragment.classification.level`` before invoking the extractor —
    the runtime enforcement of spec-classification-boundary's "LLM
    operations are domain-scoped and must never cross." A CUI fragment
    only ever sees an extractor whose ``max_classification >= 'cui'``.
    """

    name: str
    runs_on: str                  # "local" | "in_enclave" | "external_provider"
    provider_id: str | None    # "bonsai" | "openai" | None for deterministic
    logs_to: tuple[str, ...]      # ("local_audit",) | ("openai_metrics", ...)
    max_classification: str       # "unclassified" | "cui" | "secret" | ...


# v0 ordering for capability checks; tighter regimes expand this.
_CLASSIFICATION_RANK: dict[str, int] = {
    "unclassified": 0,
    "cui": 1,
    "secret": 2,
    "top_secret": 3,
}


def _can_handle(cap: ExtractorCapability, fragment_level: str) -> bool:
    fragment_rank = _CLASSIFICATION_RANK.get(fragment_level, 99)
    capability_rank = _CLASSIFICATION_RANK.get(cap.max_classification, -1)
    return capability_rank >= fragment_rank


# ---------------------------------------------------------------------------
# Protocols
# ---------------------------------------------------------------------------


class ConceptGraph(Protocol):
    """The L2 contract every backend implements."""

    def upsert_concept(self, c: Concept) -> None: ...
    def upsert_edge(self, e: ConceptEdge) -> None: ...
    def get_concept(self, concept_id: str) -> Concept | None: ...
    def neighbors(
        self, concept_id: str, *, hops: int = 1,
        edge_types: frozenset[str] | None = None,
    ) -> list[Concept]: ...
    def query(self, q: GraphQuery) -> list[Concept]: ...
    def all_concepts(self) -> Iterable[Concept]: ...
    def concept_count(self) -> int: ...
    def edge_count(self) -> int: ...


class ConceptExtractor(Protocol):
    """The extractor contract per spec-memory §5.3.

    Extensions register one or more. The runtime selects extractors
    by (a) handles match on ``cognitive_type`` and (b) capability
    match on classification.
    """

    capability: ExtractorCapability
    handles: frozenset[CognitiveType]

    def extract(self, fragment: MemoryFragment) -> list[Concept]: ...
    def link(
        self, fragment: MemoryFragment, existing: ConceptGraph,
    ) -> list[ConceptEdge]: ...


# ---------------------------------------------------------------------------
# SQLite-backed default impl (Edge / Workstation profiles)
# ---------------------------------------------------------------------------


class SQLiteConceptGraph:
    """Default ``ConceptGraph`` implementation. Zero-deps; one file.

    Same thread-local-connection pattern as ``ArtifactRegistry``'s
    SQLiteBackend so it runs under FastAPI/uvicorn worker threads
    without sqlite3.ProgrammingError.
    """

    def __init__(self, path: Path | str) -> None:
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._tls = threading.local()
        self._init_schema()

    @property
    def _conn(self) -> sqlite3.Connection:
        conn = getattr(self._tls, "conn", None)
        if conn is None:
            conn = sqlite3.connect(str(self._path))
            self._tls.conn = conn
        return conn

    def _init_schema(self) -> None:
        # Stage-3-of-L2 schema: evidence + extracted_from rows are
        # indexed siblings, not unbounded JSON blobs on the parent row.
        # Upserts become O(1); reads that need full lists become O(K)
        # where K is the matching subset.
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS concepts (
                concept_id TEXT PRIMARY KEY,
                canonical_name TEXT NOT NULL,
                confidence REAL NOT NULL DEFAULT 1.0
            );
            CREATE INDEX IF NOT EXISTS idx_concepts_name
                ON concepts(canonical_name);

            CREATE TABLE IF NOT EXISTS concept_extracted_from (
                concept_id  TEXT NOT NULL,
                fragment_id TEXT NOT NULL,
                PRIMARY KEY (concept_id, fragment_id)
            );

            CREATE TABLE IF NOT EXISTS concept_edges (
                from_concept TEXT NOT NULL,
                to_concept   TEXT NOT NULL,
                edge_type    TEXT NOT NULL,
                weight       REAL NOT NULL DEFAULT 1.0,
                PRIMARY KEY (from_concept, to_concept, edge_type)
            );
            CREATE INDEX IF NOT EXISTS idx_edges_from
                ON concept_edges(from_concept, edge_type);
            CREATE INDEX IF NOT EXISTS idx_edges_to
                ON concept_edges(to_concept, edge_type);

            CREATE TABLE IF NOT EXISTS concept_edge_evidence (
                from_concept TEXT NOT NULL,
                to_concept   TEXT NOT NULL,
                edge_type    TEXT NOT NULL,
                fragment_id  TEXT NOT NULL,
                PRIMARY KEY (from_concept, to_concept, edge_type, fragment_id)
            );
            CREATE INDEX IF NOT EXISTS idx_edge_evidence_lookup
                ON concept_edge_evidence(from_concept, to_concept, edge_type);
            """
        )
        self._conn.commit()

    # ----- Writes (upsert merges provenance; no destructive overwrites) -----

    def upsert_concept(self, c: Concept) -> None:
        # Concept identity row — INSERT OR IGNORE keeps existing
        # canonical_name + retains the strongest confidence we've ever
        # seen for this concept_id.
        cur = self._conn.execute(
            "SELECT confidence FROM concepts WHERE concept_id = ?",
            (c.concept_id,),
        )
        row = cur.fetchone()
        if row is None:
            self._conn.execute(
                "INSERT INTO concepts(concept_id, canonical_name, confidence) "
                "VALUES (?, ?, ?)",
                (c.concept_id, c.canonical_name, c.confidence),
            )
        else:
            new_confidence = max(float(row[0]), c.confidence)
            if new_confidence > float(row[0]):
                self._conn.execute(
                    "UPDATE concepts SET confidence = ? WHERE concept_id = ?",
                    (new_confidence, c.concept_id),
                )

        # Provenance rows — O(1) INSERT per fragment_id; PK dedupes
        # automatically. Cumulative cost stays linear in unique
        # fragment_ids written, never quadratic.
        for fragment_id in c.extracted_from:
            self._conn.execute(
                "INSERT OR IGNORE INTO concept_extracted_from(concept_id, "
                "fragment_id) VALUES (?, ?)",
                (c.concept_id, fragment_id),
            )
        self._conn.commit()

    def upsert_edge(self, e: ConceptEdge) -> None:
        # Edge identity row — same shape as before, minus the JSON.
        cur = self._conn.execute(
            "SELECT weight FROM concept_edges WHERE "
            "from_concept = ? AND to_concept = ? AND edge_type = ?",
            (e.from_concept, e.to_concept, e.edge_type),
        )
        row = cur.fetchone()
        if row is None:
            self._conn.execute(
                "INSERT INTO concept_edges(from_concept, to_concept, "
                "edge_type, weight) VALUES (?, ?, ?, ?)",
                (e.from_concept, e.to_concept, e.edge_type, e.weight),
            )
        else:
            new_weight = max(float(row[0]), e.weight)
            if new_weight > float(row[0]):
                self._conn.execute(
                    "UPDATE concept_edges SET weight = ? "
                    "WHERE from_concept = ? AND to_concept = ? "
                    "AND edge_type = ?",
                    (new_weight, e.from_concept, e.to_concept, e.edge_type),
                )

        # Evidence rows — O(1) INSERT per fragment_id; PK dedupes.
        for fragment_id in e.evidence:
            self._conn.execute(
                "INSERT OR IGNORE INTO concept_edge_evidence("
                "from_concept, to_concept, edge_type, fragment_id) "
                "VALUES (?, ?, ?, ?)",
                (e.from_concept, e.to_concept, e.edge_type, fragment_id),
            )
        self._conn.commit()

    # ----- Reads -----

    def get_concept(self, concept_id: str) -> Concept | None:
        cur = self._conn.execute(
            "SELECT concept_id, canonical_name, confidence "
            "FROM concepts WHERE concept_id = ?",
            (concept_id,),
        )
        row = cur.fetchone()
        if row is None:
            return None
        # Evidence join — O(K) where K is the count of fragments that
        # produced this concept. The PK index makes this efficient.
        ev_cur = self._conn.execute(
            "SELECT fragment_id FROM concept_extracted_from "
            "WHERE concept_id = ? ORDER BY fragment_id",
            (concept_id,),
        )
        extracted_from = [r[0] for r in ev_cur.fetchall()]
        return Concept(
            concept_id=row[0],
            canonical_name=row[1],
            extracted_from=extracted_from,
            confidence=float(row[2]),
        )

    def neighbors(
        self, concept_id: str, *, hops: int = 1,
        edge_types: frozenset[str] | None = None,
    ) -> list[Concept]:
        # BFS up to ``hops`` levels; deduplicate by concept_id.
        seen: set[str] = {concept_id}
        frontier: set[str] = {concept_id}
        result_ids: list[str] = []

        edge_clause = ""
        edge_params: list[Any] = []
        if edge_types:
            placeholders = ",".join("?" * len(edge_types))
            edge_clause = f" AND edge_type IN ({placeholders})"
            edge_params = list(edge_types)

        for _ in range(max(0, hops)):
            next_frontier: set[str] = set()
            for cid in frontier:
                # Outgoing: SELECT to_concept WHERE from_concept = cid
                # Incoming: SELECT from_concept WHERE to_concept = cid
                # Bind order matches placeholder order in the SQL exactly.
                bind_params: list[Any] = (
                    [cid] + edge_params + [cid] + edge_params
                )
                cur = self._conn.execute(
                    "SELECT to_concept FROM concept_edges WHERE "
                    f"from_concept = ?{edge_clause} "
                    "UNION "
                    "SELECT from_concept FROM concept_edges WHERE "
                    f"to_concept = ?{edge_clause}",
                    bind_params,
                )
                for row in cur.fetchall():
                    nb = row[0]
                    if nb not in seen:
                        seen.add(nb)
                        next_frontier.add(nb)
                        result_ids.append(nb)
            frontier = next_frontier
            if not frontier:
                break

        return self._batch_get_concepts(result_ids)

    def _batch_get_concepts(self, ids: list[str]) -> list[Concept]:
        """Fetch many concepts in two SQL queries instead of 2N.

        Used by ``neighbors`` and ``query`` — both produce a list of
        concept_ids and then need the full Concept (including the
        evidence-backed ``extracted_from``). Batching the join keeps
        the read path O(K + log N) instead of O(K log N).
        """
        if not ids:
            return []
        placeholders = ",".join("?" * len(ids))
        cur = self._conn.execute(
            "SELECT concept_id, canonical_name, confidence FROM concepts "
            f"WHERE concept_id IN ({placeholders})",
            ids,
        )
        rows = cur.fetchall()
        if not rows:
            return []
        concept_ids = [r[0] for r in rows]
        ev_cur = self._conn.execute(
            "SELECT concept_id, fragment_id FROM concept_extracted_from "
            f"WHERE concept_id IN ({placeholders}) "
            "ORDER BY concept_id, fragment_id",
            concept_ids,
        )
        evidence_by_concept: dict[str, list[str]] = {cid: [] for cid in concept_ids}
        for cid, fid in ev_cur.fetchall():
            evidence_by_concept[cid].append(fid)

        # Preserve the input order so callers that rank by BFS depth keep
        # the order they expect.
        by_id = {
            r[0]: Concept(
                concept_id=r[0],
                canonical_name=r[1],
                extracted_from=evidence_by_concept.get(r[0], []),
                confidence=float(r[2]),
            )
            for r in rows
        }
        return [by_id[i] for i in ids if i in by_id]

    def query(self, q: GraphQuery) -> list[Concept]:
        results: dict[str, Concept] = {}
        for seed in q.seed_concepts:
            for n in self.neighbors(
                seed, hops=q.max_hops, edge_types=q.edge_types,
            ):
                if n.concept_id not in results:
                    results[n.concept_id] = n
                if len(results) >= q.limit:
                    return list(results.values())
        return list(results.values())

    def all_concepts(self) -> Iterable[Concept]:
        cur = self._conn.execute(
            "SELECT concept_id, canonical_name, confidence "
            "FROM concepts ORDER BY canonical_name"
        )
        rows = cur.fetchall()
        for row in rows:
            ev_cur = self._conn.execute(
                "SELECT fragment_id FROM concept_extracted_from "
                "WHERE concept_id = ? ORDER BY fragment_id",
                (row[0],),
            )
            extracted_from = [r[0] for r in ev_cur.fetchall()]
            yield Concept(
                concept_id=row[0],
                canonical_name=row[1],
                extracted_from=extracted_from,
                confidence=float(row[2]),
            )

    def concept_count(self) -> int:
        cur = self._conn.execute("SELECT COUNT(*) FROM concepts")
        return int(cur.fetchone()[0])

    def edge_count(self) -> int:
        cur = self._conn.execute("SELECT COUNT(*) FROM concept_edges")
        return int(cur.fetchone()[0])


# ---------------------------------------------------------------------------
# Reference extractor — deterministic keyword extractor for episodic text
# ---------------------------------------------------------------------------


# Stop words copied from classroom_interaction's topic histogram so the
# Stage 2 extractor produces concepts equivalent to the existing
# topic-extraction logic (and thus is a strict upgrade, not a divergence).
_STOPWORDS = frozenset({
    "the", "a", "an", "and", "or", "of", "to", "is", "are", "was", "were",
    "what", "who", "where", "why", "how", "when", "this", "that", "these",
    "those", "does", "do", "did", "in", "on", "for", "with", "by", "from",
    "as", "at", "be", "can", "it", "its", "if", "so", "such", "not", "no",
    "tell", "me", "my", "your", "you", "we", "us", "about", "have", "has",
})


_WORD = re.compile(r"[a-z][a-z0-9']{2,}")


@dataclass
class DeterministicTextExtractor:
    """Stop-word-filtered keyword extractor. No LLM; fully deterministic.

    For Stage 2 we ship a deterministic baseline that produces concepts
    from question text + material text. It runs everywhere (including
    air-gapped + classified contexts) without provider calls. LLM-based
    extractors slot in alongside for richer extraction at higher
    classification capability — same protocol.

    Edge inference: any two concepts co-occurring in the same fragment
    get a ``co_occurs`` edge weighted by their shared appearances.
    """

    capability: ExtractorCapability = field(
        default_factory=lambda: ExtractorCapability(
            name="deterministic_text",
            runs_on="local",
            provider_id=None,
            logs_to=("local_audit",),
            # Deterministic local extraction is safe up to top_secret —
            # no data leaves the machine.
            max_classification="top_secret",
        )
    )

    @property
    def handles(self) -> frozenset:
        # Lazy import to avoid circular dependency at module load.
        from axiom.memory.fragment import CognitiveType
        return frozenset({CognitiveType.EPISODIC, CognitiveType.RESOURCE})

    def _tokens_for(self, fragment: MemoryFragment) -> list[str]:
        """Pull text fields off the fragment shape we know about."""
        text_parts: list[str] = []
        content = fragment.content or {}
        for key in ("question", "text", "title", "summary", "body"):
            v = content.get(key)
            if isinstance(v, str):
                text_parts.append(v)
        joined = " ".join(text_parts).lower()
        return [
            t for t in _WORD.findall(joined)
            if t not in _STOPWORDS
        ]

    def extract(self, fragment: MemoryFragment) -> list[Concept]:
        tokens = self._tokens_for(fragment)
        if not tokens:
            return []
        seen: dict[str, int] = {}
        for t in tokens:
            seen[t] = seen.get(t, 0) + 1
        return [
            Concept(
                concept_id=canonical_concept_id(name),
                canonical_name=name,
                extracted_from=[fragment.id],
                confidence=min(1.0, 0.5 + 0.1 * count),
            )
            for name, count in seen.items()
        ]

    def link(
        self, fragment: MemoryFragment, existing: ConceptGraph,
    ) -> list[ConceptEdge]:
        concepts = self.extract(fragment)
        edges: list[ConceptEdge] = []
        ids = [c.concept_id for c in concepts]
        # Co-occurrence edges. Symmetric; we emit one edge per
        # unordered pair to keep storage bounded; neighbors() returns
        # both directions thanks to the UNION query.
        for i, a in enumerate(ids):
            for b in ids[i + 1:]:
                if a == b:
                    continue
                f, t = (a, b) if a < b else (b, a)
                edges.append(ConceptEdge(
                    from_concept=f,
                    to_concept=t,
                    edge_type="co_occurs",
                    weight=1.0,
                    evidence=[fragment.id],
                ))
        return edges


# ---------------------------------------------------------------------------
# Extractor registry + composition-service hook
# ---------------------------------------------------------------------------


@dataclass
class ExtractorRegistry:
    """Holds the active extractors. CompositionService holds an instance
    and runs ``run_for_fragment`` on every write.

    Per spec-memory I11 + I12: classification-aware selection (an
    extractor whose capability < fragment level is never invoked) +
    multi-extractor conflict resolution (deterministic extractors win
    on overlap; here, all upserts merge so there's no conflict to
    resolve at this layer — the upsert primitive is idempotent).
    """

    graph: ConceptGraph
    extractors: list[ConceptExtractor] = field(default_factory=list)

    def register(self, extractor: ConceptExtractor) -> None:
        self.extractors.append(extractor)

    def run_for_fragment(self, fragment: MemoryFragment) -> dict:
        """Invoke every applicable extractor, upsert results into the graph.

        Returns a summary dict for caller observability:
        ``{"concepts": int, "edges": int, "extractors_run": [name, ...],
           "extractors_skipped_classification": [name, ...]}``.
        """
        concepts_total = 0
        edges_total = 0
        ran: list[str] = []
        skipped: list[str] = []

        fragment_level = (
            fragment.classification.level
            if fragment.classification is not None
            else "unclassified"
        )

        for extractor in self.extractors:
            # Type filter (protocol-declared)
            if fragment.cognitive_type not in extractor.handles:
                continue
            # Classification gate (spec-memory I11)
            if not _can_handle(extractor.capability, fragment_level):
                skipped.append(extractor.capability.name)
                continue

            try:
                concepts = extractor.extract(fragment)
                edges = extractor.link(fragment, self.graph)
            except Exception:
                # Extractor failure must not break L1 writes. Log to
                # caller observability via summary; production wires a
                # real logger later.
                skipped.append(extractor.capability.name + ":error")
                continue

            for c in concepts:
                self.graph.upsert_concept(c)
            for e in edges:
                self.graph.upsert_edge(e)
            concepts_total += len(concepts)
            edges_total += len(edges)
            ran.append(extractor.capability.name)

        return {
            "concepts": concepts_total,
            "edges": edges_total,
            "extractors_run": ran,
            "extractors_skipped_classification": skipped,
        }


def rebuild_graph_from_log(
    *,
    graph: ConceptGraph,
    registry: ExtractorRegistry,
    fragments: Iterable[MemoryFragment],
) -> dict:
    """Replay an event log into a (typically empty) graph.

    Implements the I8 invariant: the graph at any point in time is fully
    derivable from L1. Used for migrations, snapshot rebuilding,
    federation onboarding (a peer joins, replays, builds its own copy).

    Returns aggregate stats across all fragments processed.
    """
    total = {"fragments": 0, "concepts": 0, "edges": 0}
    for f in fragments:
        summary = registry.run_for_fragment(f)
        total["fragments"] += 1
        total["concepts"] += summary["concepts"]
        total["edges"] += summary["edges"]
    return total


__all__ = [
    "Concept",
    "ConceptEdge",
    "ConceptExtractor",
    "ConceptGraph",
    "DeterministicTextExtractor",
    "ExtractorCapability",
    "ExtractorRegistry",
    "GraphQuery",
    "SQLiteConceptGraph",
    "canonical_concept_id",
    "rebuild_graph_from_log",
]
