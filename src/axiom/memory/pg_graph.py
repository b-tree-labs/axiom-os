# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Postgres concept graph (ADR-174): the same ConceptGraph contract as the SQLite one.

Merge semantics are preserved: a concept's name never changes after first insert,
confidence and edge weight keep the maximum ever seen, provenance and evidence rows
are insert-if-absent. A *scope* names one graph.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError

from axiom.memory.graph import Concept, ConceptEdge, GraphQuery
from axiom.memory.pg_models import (
    ConceptEdgeEvidenceRow,
    ConceptEdgeRow,
    ConceptRow,
    ConceptSourceRow,
)
from axiom.memory.pg_store import ensure_provisioned, session_scope


def _add_if_absent(session: Any, model: Any, key: tuple, **fields: Any) -> None:
    """Insert unless the primary key exists; a lost race is not an error."""
    if session.get(model, key) is not None:
        return
    try:
        with session.begin_nested():
            session.add(model(**fields))
    except IntegrityError:
        pass


class PostgresConceptGraph:
    def __init__(self, scope: str) -> None:
        if not scope:
            raise ValueError("a graph scope is required")
        self.scope = scope

    # ----- writes -----

    def upsert_concept(self, c: Concept) -> None:
        with session_scope() as s:
            row = s.get(ConceptRow, (self.scope, c.concept_id))
            if row is None:
                _add_if_absent(
                    s,
                    ConceptRow,
                    (self.scope, c.concept_id),
                    scope=self.scope,
                    concept_id=c.concept_id,
                    canonical_name=c.canonical_name,
                    confidence=c.confidence,
                )
                row = s.get(ConceptRow, (self.scope, c.concept_id))
            if row is not None and c.confidence > float(row.confidence):
                row.confidence = c.confidence
            for fragment_id in c.extracted_from:
                _add_if_absent(
                    s,
                    ConceptSourceRow,
                    (self.scope, c.concept_id, fragment_id),
                    scope=self.scope,
                    concept_id=c.concept_id,
                    fragment_id=fragment_id,
                )
            s.commit()

    def upsert_edge(self, e: ConceptEdge) -> None:
        key = (self.scope, e.from_concept, e.to_concept, e.edge_type)
        with session_scope() as s:
            row = s.get(ConceptEdgeRow, key)
            if row is None:
                _add_if_absent(
                    s,
                    ConceptEdgeRow,
                    key,
                    scope=self.scope,
                    from_concept=e.from_concept,
                    to_concept=e.to_concept,
                    edge_type=e.edge_type,
                    weight=e.weight,
                )
                row = s.get(ConceptEdgeRow, key)
            if row is not None and e.weight > float(row.weight):
                row.weight = e.weight
            for fragment_id in e.evidence:
                _add_if_absent(
                    s,
                    ConceptEdgeEvidenceRow,
                    (*key, fragment_id),
                    scope=self.scope,
                    from_concept=e.from_concept,
                    to_concept=e.to_concept,
                    edge_type=e.edge_type,
                    fragment_id=fragment_id,
                )
            s.commit()

    # ----- reads -----

    def _sources(self, s: Any, ids: list[str]) -> dict[str, list[str]]:
        out: dict[str, list[str]] = {i: [] for i in ids}
        if not ids:
            return out
        rows = s.execute(
            select(ConceptSourceRow.concept_id, ConceptSourceRow.fragment_id)
            .where(ConceptSourceRow.scope == self.scope, ConceptSourceRow.concept_id.in_(ids))
            .order_by(ConceptSourceRow.concept_id, ConceptSourceRow.fragment_id)
        )
        for cid, fid in rows:
            out[cid].append(fid)
        return out

    def _concepts(self, s: Any, ids: list[str]) -> list[Concept]:
        """Concepts for ``ids``, in the order given (callers rank by BFS depth)."""
        if not ids:
            return []
        rows = s.execute(
            select(ConceptRow).where(ConceptRow.scope == self.scope, ConceptRow.concept_id.in_(ids))
        ).scalars()
        by_id = {r.concept_id: r for r in rows}
        sources = self._sources(s, list(by_id))
        return [
            Concept(
                concept_id=i,
                canonical_name=by_id[i].canonical_name,
                extracted_from=sources.get(i, []),
                confidence=float(by_id[i].confidence),
            )
            for i in ids
            if i in by_id
        ]

    def get_concept(self, concept_id: str) -> Concept | None:
        with session_scope() as s:
            found = self._concepts(s, [concept_id])
            return found[0] if found else None

    def neighbors(
        self,
        concept_id: str,
        *,
        hops: int = 1,
        edge_types: frozenset[str] | None = None,
    ) -> list[Concept]:
        seen: set[str] = {concept_id}
        frontier: set[str] = {concept_id}
        ordered: list[str] = []
        with session_scope() as s:
            for _ in range(max(0, hops)):
                if not frontier:
                    break
                clauses: list[Any] = [
                    ConceptEdgeRow.scope == self.scope,
                    or_(
                        ConceptEdgeRow.from_concept.in_(frontier),
                        ConceptEdgeRow.to_concept.in_(frontier),
                    ),
                ]
                if edge_types:
                    clauses.append(ConceptEdgeRow.edge_type.in_(edge_types))
                next_frontier: set[str] = set()
                rows = s.execute(
                    select(ConceptEdgeRow.from_concept, ConceptEdgeRow.to_concept)
                    .where(*clauses)
                    .order_by(ConceptEdgeRow.from_concept, ConceptEdgeRow.to_concept)
                )
                for a, b in rows:
                    for nb in ((b,) if a in frontier else ()) + ((a,) if b in frontier else ()):
                        if nb not in seen:
                            seen.add(nb)
                            next_frontier.add(nb)
                            ordered.append(nb)
                frontier = next_frontier
            return self._concepts(s, ordered)

    def query(self, q: GraphQuery) -> list[Concept]:
        results: dict[str, Concept] = {}
        for seed in q.seed_concepts:
            for n in self.neighbors(seed, hops=q.max_hops, edge_types=q.edge_types):
                results.setdefault(n.concept_id, n)
                if len(results) >= q.limit:
                    return list(results.values())
        return list(results.values())

    def all_concepts(self) -> Iterable[Concept]:
        with session_scope() as s:
            rows = list(
                s.execute(
                    select(ConceptRow)
                    .where(ConceptRow.scope == self.scope)
                    .order_by(ConceptRow.canonical_name)
                ).scalars()
            )
            sources = self._sources(s, [r.concept_id for r in rows])
            out = [
                Concept(
                    concept_id=r.concept_id,
                    canonical_name=r.canonical_name,
                    extracted_from=sources[r.concept_id],
                    confidence=float(r.confidence),
                )
                for r in rows
            ]
        return iter(out)

    def concept_count(self) -> int:
        with session_scope() as s:
            return int(
                s.execute(
                    select(func.count())
                    .select_from(ConceptRow)
                    .where(ConceptRow.scope == self.scope)
                ).scalar_one()
            )

    def edge_count(self) -> int:
        with session_scope() as s:
            return int(
                s.execute(
                    select(func.count())
                    .select_from(ConceptEdgeRow)
                    .where(ConceptEdgeRow.scope == self.scope)
                ).scalar_one()
            )


def open_graph(scope: str) -> PostgresConceptGraph:
    ensure_provisioned()
    return PostgresConceptGraph(scope)
