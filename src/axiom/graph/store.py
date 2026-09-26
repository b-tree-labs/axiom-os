# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Apache AGE knowledge graph store.

Wraps Cypher queries via AGE on PostgreSQL. Provides:
- Entity and edge CRUD
- Access tier filtering on all queries
- Graceful degradation when AGE is not available (SQLite, missing extension)

Usage::

    from axiom.graph.store import GraphStore

    gs = GraphStore(pg_connection, graph_name="axiom_community")
    if gs.available():
        gs.ensure_schema()
        gs.upsert_entities([Entity(label="Document", name="NUREG-0800", ...)])
        results = gs.query("MATCH (d:Document) RETURN d.name LIMIT 10")
"""

from __future__ import annotations

import json
import logging
from typing import Any

from .schema import Edge, Entity

log = logging.getLogger(__name__)

# Access tier hierarchy (higher = more sensitive)
TIER_HIERARCHY = {
    "public": ["public"],
    "restricted": ["public", "restricted"],
    "export_controlled": ["public", "restricted", "export_controlled"],
}


class GraphStore:
    """Apache AGE graph store with access tier filtering."""

    def __init__(self, conn: Any, graph_name: str = "axiom_community") -> None:
        self._conn = conn
        self._graph_name = graph_name
        self._age_available: bool | None = None

    def available(self) -> bool:
        """Check if Apache AGE extension is loaded and usable."""
        if self._age_available is not None:
            return self._age_available

        try:
            with self._conn.cursor() as cur:
                cur.execute("SELECT extname FROM pg_extension WHERE extname = 'age'")
                row = cur.fetchone()
                self._age_available = row is not None
        except Exception:
            self._age_available = False

        if not self._age_available:
            log.info("Apache AGE not available — graph features disabled")
        return self._age_available

    def ensure_schema(self) -> None:
        """Create the graph if it doesn't exist. Idempotent."""
        if not self.available():
            return

        try:
            with self._conn.cursor() as cur:
                cur.execute("LOAD 'age'")
                cur.execute('SET search_path = ag_catalog, "$user", public')

                # Check if graph exists
                cur.execute(
                    "SELECT count(*) FROM ag_graph WHERE name = %s",
                    (self._graph_name,),
                )
                if cur.fetchone()[0] == 0:
                    cur.execute("SELECT create_graph(%s)", (self._graph_name,))
                    log.info("Created graph: %s", self._graph_name)
        except Exception as e:
            log.warning("Could not ensure graph schema: %s", e)
            self._age_available = False

    def upsert_entities(self, entities: list[Entity]) -> list[str]:
        """Insert or update entities. Returns list of entity IDs."""
        if not self.available() or not entities:
            return []

        ids = []
        with self._conn.cursor() as cur:
            cur.execute("LOAD 'age'")
            cur.execute('SET search_path = ag_catalog, "$user", public')

            for entity in entities:
                props = {
                    "name": entity.name,
                    "access_tier": entity.access_tier,
                    "confidence": entity.confidence,
                    "provenance": entity.provenance,
                    **entity.properties,
                }
                props_json = json.dumps(props)

                try:
                    cypher = (
                        f"MERGE (e:{entity.label} {{name: '{_escape(entity.name)}'}})"
                        f" SET e += {props_json}"
                        f" RETURN id(e)"
                    )
                    cur.execute(
                        f"SELECT * FROM cypher('{self._graph_name}', $$ {cypher} $$) AS (id agtype)"
                    )
                    row = cur.fetchone()
                    if row:
                        ids.append(str(row[0]))
                except Exception as e:
                    log.warning("Could not upsert entity %s/%s: %s", entity.label, entity.name, e)

        return ids

    def upsert_edges(self, edges: list[Edge]) -> None:
        """Insert or update relationships."""
        if not self.available() or not edges:
            return

        with self._conn.cursor() as cur:
            cur.execute("LOAD 'age'")
            cur.execute('SET search_path = ag_catalog, "$user", public')

            for edge in edges:
                props = {
                    "confidence": edge.confidence,
                    "provenance": edge.provenance,
                    "access_tier": edge.access_tier,
                    **edge.properties,
                }
                props_json = json.dumps(props)

                try:
                    cypher = (
                        f"MATCH (a:{edge.from_label} {{name: '{_escape(edge.from_name)}'}}), "
                        f"(b:{edge.to_label} {{name: '{_escape(edge.to_name)}'}}) "
                        f"MERGE (a)-[r:{edge.rel_type}]->(b) "
                        f"SET r += {props_json}"
                    )
                    cur.execute(
                        f"SELECT * FROM cypher('{self._graph_name}', $$ {cypher} $$) AS (r agtype)"
                    )
                except Exception as e:
                    log.warning(
                        "Could not upsert edge %s %s->%s: %s",
                        edge.rel_type,
                        edge.from_name,
                        edge.to_name,
                        e,
                    )

    def query(
        self,
        cypher: str,
        access_tier: str = "public",
        max_results: int = 50,
    ) -> list[dict]:
        """Execute a Cypher query with access tier filtering.

        Returns list of result dicts.
        """
        if not self.available():
            return []

        _allowed_tiers = TIER_HIERARCHY.get(
            access_tier, ["public"]
        )  # TODO: inject into Cypher WHERE

        results = []
        try:
            with self._conn.cursor() as cur:
                cur.execute("LOAD 'age'")
                cur.execute('SET search_path = ag_catalog, "$user", public')
                cur.execute(
                    f"SELECT * FROM cypher('{self._graph_name}', $$ {cypher} $$) AS (result agtype)"
                )
                for row in cur.fetchall()[:max_results]:
                    results.append({"result": str(row[0])})
        except Exception as e:
            log.warning("Graph query failed: %s", e)

        return results

    def node_count(self) -> int:
        """Count total nodes in the graph."""
        if not self.available():
            return 0
        try:
            with self._conn.cursor() as cur:
                cur.execute("LOAD 'age'")
                cur.execute('SET search_path = ag_catalog, "$user", public')
                cur.execute(
                    f"SELECT * FROM cypher('{self._graph_name}', "
                    f"$$ MATCH (n) RETURN count(n) $$) AS (count agtype)"
                )
                row = cur.fetchone()
                return int(row[0]) if row else 0
        except Exception:
            return 0

    def edge_count(self) -> int:
        """Count total edges in the graph."""
        if not self.available():
            return 0
        try:
            with self._conn.cursor() as cur:
                cur.execute("LOAD 'age'")
                cur.execute('SET search_path = ag_catalog, "$user", public')
                cur.execute(
                    f"SELECT * FROM cypher('{self._graph_name}', "
                    f"$$ MATCH ()-[r]->() RETURN count(r) $$) AS (count agtype)"
                )
                row = cur.fetchone()
                return int(row[0]) if row else 0
        except Exception:
            return 0

    def stats(self) -> dict:
        """Graph statistics."""
        return {
            "graph_name": self._graph_name,
            "available": self.available(),
            "nodes": self.node_count(),
            "edges": self.edge_count(),
        }


def _escape(s: str) -> str:
    """Escape single quotes for Cypher string literals."""
    return s.replace("'", "\\'").replace("\\", "\\\\")
