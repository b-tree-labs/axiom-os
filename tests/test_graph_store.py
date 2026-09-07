# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tests for the Apache AGE knowledge graph store.

TDD: tests before implementation.
Graph store wraps AGE Cypher queries with access tier filtering
and graceful degradation when AGE is not available.
"""

from __future__ import annotations

from unittest.mock import MagicMock


class TestGraphStoreUnit:
    """Unit tests — no DB required."""

    def test_importable(self):
        from axiom.graph.store import GraphStore
        assert GraphStore is not None

    def test_entity_types_defined(self):
        """Core entity types must be defined."""
        from axiom.graph.schema import CORE_ENTITY_TYPES
        names = {e.label for e in CORE_ENTITY_TYPES}
        assert "Document" in names
        assert "Component" in names
        assert "Procedure" in names
        assert "Person" in names
        assert "Code" in names
        assert "Material" in names
        assert "Concept" in names
        assert "Fact" in names

    def test_relationship_types_defined(self):
        """Core relationship types must be defined."""
        from axiom.graph.schema import CORE_RELATIONSHIP_TYPES
        names = {r.rel_type for r in CORE_RELATIONSHIP_TYPES}
        assert "REFERENCES" in names
        assert "DESCRIBES" in names
        assert "GOVERNS" in names
        assert "AUTHORED_BY" in names

    def test_entity_dataclass(self):
        from axiom.graph.schema import Entity
        e = Entity(label="Document", name="NUREG-0800", properties={"title": "SRP"})
        assert e.label == "Document"
        assert e.name == "NUREG-0800"

    def test_edge_dataclass(self):
        from axiom.graph.schema import Edge
        e = Edge(
            rel_type="REFERENCES",
            from_name="doc1",
            from_label="Document",
            to_name="doc2",
            to_label="Document",
            confidence=0.95,
        )
        assert e.rel_type == "REFERENCES"
        assert e.confidence == 0.95

    def test_graph_store_available_without_age(self):
        """GraphStore.available() returns False when AGE not loaded."""
        from axiom.graph.store import GraphStore

        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cursor)
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
        mock_cursor.execute.side_effect = Exception("extension not found")

        gs = GraphStore(mock_conn, graph_name="test")
        assert gs.available() is False

    def test_domain_agnostic(self):
        """Core entity types must NOT include domain-specific types."""
        from axiom.graph.schema import CORE_ENTITY_TYPES
        names = {e.label for e in CORE_ENTITY_TYPES}
        # These are domain consumer types — must NOT be in core
        assert "Reactor" not in names
        assert "FuelElement" not in names
        assert "Isotope" not in names

    def test_entity_type_registry(self):
        """Entity type registry supports runtime extension."""
        from axiom.graph.schema import EntityType, EntityTypeRegistry

        registry = EntityTypeRegistry()
        # Core types are pre-loaded
        assert registry.get("Document") is not None

        # Domain extension can register new types
        registry.register(EntityType(label="Reactor", parent="Component", properties=["thermal_power_mw"]))
        assert registry.get("Reactor") is not None
        assert registry.get("Reactor").parent == "Component"
