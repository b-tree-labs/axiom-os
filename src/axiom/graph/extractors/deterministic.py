# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Stage 1: Deterministic entity extraction — no LLM required.

Extracts entities and relationships from document text using
regex patterns, heading structure, and metadata parsing.
Operates on FULL document text, not chunks.

Extracts:
- Document cross-references (NUREG, CFR, ORNL, IAEA, etc.)
- Persons (author lines in headers)
- Section boundaries (headings, regulatory sections)
- REFERENCES edges between documents
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from axiom.graph.schema import Edge, Entity
from axiom.rag.semantic_chunker import SemanticBoundary, detect_boundaries


@dataclass
class ExtractionResult:
    """Output of a deterministic extraction pass."""

    entities: list[Entity] = field(default_factory=list)
    edges: list[Edge] = field(default_factory=list)
    boundaries: list[SemanticBoundary] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Cross-reference patterns
# ---------------------------------------------------------------------------

_CROSS_REF_PATTERNS = [
    # NRC documents
    (r"NUREG-CR-\d+", "Document"),
    (r"NUREG-\d+", "Document"),
    (r"Reg(?:ulatory\s+)?Guide[\s-]+\d+\.\d+", "Procedure"),
    # Code of Federal Regulations
    (r"10\s*CFR\s+\d+\.\d+", "Procedure"),
    (r"10\s*CFR\s+Part\s+\d+", "Procedure"),
    # ORNL reports
    (r"ORNL-TM-\d+", "Document"),
    (r"ORNL-CF-\d+-\d+-\d+", "Document"),
    (r"ORNL-\d+", "Document"),
    # ANL, BNL, INL reports
    (r"ANL-\d+", "Document"),
    (r"BNL-\d+", "Document"),
    (r"INL/EXT-\d+-\d+", "Document"),
    # IAEA
    (r"IAEA-SSR-\d+", "Procedure"),
    (r"IAEA-TECDOC-\d+", "Document"),
    # DOE
    (r"DOE-STD-\d+-\d+", "Procedure"),
    # EIR (Swiss)
    (r"EIR-\d+", "Document"),
    # PNL (Pacific Northwest)
    (r"PNL-\d+", "Document"),
]


def extract_cross_references(text: str) -> list[Entity]:
    """Extract document cross-references from text."""
    entities = []
    seen = set()

    for pattern, label in _CROSS_REF_PATTERNS:
        for match in re.finditer(pattern, text):
            name = match.group(0).strip()
            if name not in seen:
                seen.add(name)
                entities.append(
                    Entity(
                        label=label,
                        name=name,
                        provenance="extracted",
                        confidence=1.0,
                    )
                )

    return entities


# ---------------------------------------------------------------------------
# Person extraction (from document header)
# ---------------------------------------------------------------------------

_PERSON_PATTERNS = [
    # "M. W. Rosenthal, Program Director"
    r"([A-Z]\.\s*[A-Z]?\.\s*[A-Z][a-z]+(?:\s+[A-Z][a-z]+)?),\s*([\w\s]+Director|[\w\s]+Associate)",
    # "John Smith, Author"
    r"([A-Z][a-z]+\s+[A-Z][a-z]+),\s*(Author|Editor|Reviewer|Contributor)",
]


def extract_persons(text: str) -> list[Entity]:
    """Extract person entities from document text."""
    entities = []
    seen = set()

    # Only search the first ~2000 chars (header area)
    header = text[:2000]

    for pattern in _PERSON_PATTERNS:
        for match in re.finditer(pattern, header):
            name = match.group(1).strip()
            role = match.group(2).strip() if match.lastindex >= 2 else ""
            if name not in seen:
                seen.add(name)
                entities.append(
                    Entity(
                        label="Person",
                        name=name,
                        properties={"role": role},
                        provenance="extracted",
                        confidence=0.9,
                    )
                )

    return entities


# ---------------------------------------------------------------------------
# Section boundary extraction
# ---------------------------------------------------------------------------


def extract_section_boundaries(text: str) -> list[SemanticBoundary]:
    """Extract section boundaries from document text."""
    return detect_boundaries(text)


# ---------------------------------------------------------------------------
# Full extraction pipeline
# ---------------------------------------------------------------------------


def extract_from_document(
    text: str,
    source_path: str,
    source_type: str = "markdown",
) -> ExtractionResult:
    """Stage 1: deterministic extraction from full document text.

    Returns entities, edges, and semantic boundaries.
    """
    result = ExtractionResult()

    # Document entity for the source itself
    result.entities.append(
        Entity(
            label="Document",
            name=source_path,
            properties={"source_type": source_type},
            source_path=source_path,
            provenance="extracted",
            confidence=1.0,
        )
    )

    # Cross-references → Document/Procedure entities + REFERENCES edges
    refs = extract_cross_references(text)
    for ref in refs:
        result.entities.append(ref)
        result.edges.append(
            Edge(
                rel_type="REFERENCES",
                from_name=source_path,
                from_label="Document",
                to_name=ref.name,
                to_label=ref.label,
                confidence=1.0,
                provenance="extracted",
            )
        )

    # Persons
    persons = extract_persons(text)
    for person in persons:
        result.entities.append(person)
        result.edges.append(
            Edge(
                rel_type="AUTHORED_BY",
                from_name=source_path,
                from_label="Document",
                to_name=person.name,
                to_label="Person",
                confidence=0.9,
                provenance="extracted",
            )
        )

    # Section boundaries
    result.boundaries = extract_section_boundaries(text)

    return result
