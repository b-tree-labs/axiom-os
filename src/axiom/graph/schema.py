# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Knowledge graph schema — entity types, relationship types, and registry.

Domain-agnostic core types. Domain extensions (e.g., a consumer layer)
register additional types at runtime via EntityTypeRegistry.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class EntityType:
    """A vertex label in the knowledge graph."""

    label: str
    parent: str | None = None
    properties: list[str] = field(default_factory=list)
    description: str = ""


@dataclass
class RelationshipType:
    """An edge type in the knowledge graph."""

    rel_type: str
    from_label: str
    to_label: str
    properties: list[str] = field(default_factory=list)
    description: str = ""


@dataclass
class Entity:
    """A concrete entity instance."""

    label: str
    name: str
    properties: dict = field(default_factory=dict)
    entity_id: str = ""
    access_tier: str = "public"
    confidence: float = 1.0
    provenance: str = "extracted"
    source_path: str = ""
    source_chunk_id: int | None = None


@dataclass
class Edge:
    """A concrete relationship instance."""

    rel_type: str
    from_name: str
    from_label: str
    to_name: str
    to_label: str
    properties: dict = field(default_factory=dict)
    confidence: float = 1.0
    provenance: str = "extracted"
    access_tier: str = "public"
    source_chunk_id: int | None = None


# ---------------------------------------------------------------------------
# Core entity types (domain-agnostic)
# ---------------------------------------------------------------------------

CORE_ENTITY_TYPES = [
    EntityType(
        "Document",
        description="An indexed document in the RAG store",
        properties=["source_path", "title", "corpus", "access_tier", "content_hash"],
    ),
    EntityType(
        "Component",
        description="A physical or logical component",
        properties=["name", "aliases", "access_tier"],
    ),
    EntityType(
        "Procedure",
        description="An operational procedure, standard, or regulation",
        properties=["name", "doc_number", "revision", "access_tier"],
    ),
    EntityType(
        "Person", description="A named individual", properties=["name", "role", "organization"]
    ),
    EntityType(
        "Code",
        description="A simulation code, software tool, or library",
        properties=["name", "version", "language", "repository"],
    ),
    EntityType(
        "Material",
        description="A physical material or substance",
        properties=["name", "formula", "access_tier"],
    ),
    EntityType(
        "Concept",
        description="An abstract concept, theory, or methodology",
        properties=["name", "domain_tags"],
    ),
    EntityType(
        "Fact",
        description="A validated knowledge fact from the maturity pipeline",
        properties=["fact_id", "proposition", "confidence", "maturity_layer"],
    ),
]

CORE_RELATIONSHIP_TYPES = [
    RelationshipType("REFERENCES", "Document", "Document", properties=["section", "context"]),
    RelationshipType("DESCRIBES", "Document", "Component", properties=["section", "context"]),
    RelationshipType("GOVERNS", "Procedure", "Component", properties=["requirement_type"]),
    RelationshipType("AUTHORED_BY", "Document", "Person", properties=["role"]),
    RelationshipType("USES", "Document", "Code", properties=["version", "context"]),
    RelationshipType("COMPOSED_OF", "Component", "Material", properties=["fraction", "region"]),
    RelationshipType("DEPENDS_ON", "Component", "Component", properties=["dependency_type"]),
    RelationshipType("VALIDATES", "Fact", "Document", properties=["chunk_ids"]),
    RelationshipType("CONTRADICTS", "Fact", "Fact", properties=["confidence", "resolution_status"]),
    RelationshipType("SUPERSEDES", "Document", "Document", properties=["effective_date"]),
]


# ---------------------------------------------------------------------------
# Entity type registry (extensible at runtime)
# ---------------------------------------------------------------------------


class EntityTypeRegistry:
    """Registry of entity and relationship types.

    Pre-loaded with core types. Domain extensions register additional
    types at runtime.
    """

    def __init__(self) -> None:
        self._entity_types: dict[str, EntityType] = {e.label: e for e in CORE_ENTITY_TYPES}
        self._rel_types: dict[str, RelationshipType] = {
            r.rel_type: r for r in CORE_RELATIONSHIP_TYPES
        }

    def register(self, entity_type: EntityType) -> None:
        """Register a new entity type (domain extension)."""
        self._entity_types[entity_type.label] = entity_type

    def register_relationship(self, rel_type: RelationshipType) -> None:
        """Register a new relationship type."""
        self._rel_types[rel_type.rel_type] = rel_type

    def get(self, label: str) -> EntityType | None:
        return self._entity_types.get(label)

    def get_relationship(self, rel_type: str) -> RelationshipType | None:
        return self._rel_types.get(rel_type)

    def all_entity_types(self) -> list[EntityType]:
        return list(self._entity_types.values())

    def all_relationship_types(self) -> list[RelationshipType]:
        return list(self._rel_types.values())

    def entity_labels(self) -> list[str]:
        return list(self._entity_types.keys())

    def relationship_types(self) -> list[str]:
        return list(self._rel_types.keys())
