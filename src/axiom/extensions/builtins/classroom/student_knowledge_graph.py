# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Per-student knowledge graph (#27).

At any point in the course — especially at graduation — a student
should be able to point at *what they learned*. Traditional
transcripts don't show it; interaction history is fragmented across
tools; AI chats vanish.

This module walks the student's fragments (episodic traces, semantic
quiz scores, SCAN signals about them, help tickets they raised) and
builds a knowledge graph with:
- **Concept nodes** — topics the student engaged with
- **Mastery annotation** — derived from quiz scores on each concept
- **Struggle annotation** — from SCAN stuck-signals + help tickets
- **Encounter edges** — when the student first/last touched a concept

The graph becomes part of the student's `.axiompack` harvest
(ADR-026 transfer ceremony) — a durable, signed, portable record
of their learning trajectory that survives any institutional change.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from axiom.memory.composition import CompositionService


@dataclass
class ConceptNode:
    """A topic/concept the student encountered."""

    concept: str
    first_encountered: str | None = None
    last_reviewed: str | None = None
    encounter_count: int = 0
    mastery_score: float | None = None       # from quizzes
    struggle_count: int = 0                     # from signals + tickets
    quiz_attempts: int = 0


@dataclass
class KnowledgeGraph:
    """A student's learning graph — nodes + metadata."""

    student_id: str
    classroom_id: str | None = None
    course_id: str | None = None
    nodes: dict[str, ConceptNode] = field(default_factory=dict)

    def summary(self) -> dict:
        """Coarse summary statistics for the graduation artifact."""
        mastered = [
            n for n in self.nodes.values()
            if n.mastery_score is not None and n.mastery_score >= 0.8
        ]
        struggling = [n for n in self.nodes.values() if n.struggle_count > 0]
        return {
            "total_concepts": len(self.nodes),
            "mastered_concepts": len(mastered),
            "struggling_concepts": len(struggling),
            "top_strengths": [n.concept for n in mastered[:5]],
            "active_focus_areas": [n.concept for n in struggling[:5]],
        }

    def to_dict(self) -> dict:
        """JSON-safe serialization for the .axiompack bundle."""
        return {
            "student_id": self.student_id,
            "classroom_id": self.classroom_id,
            "course_id": self.course_id,
            "nodes": {
                concept: {
                    "first_encountered": n.first_encountered,
                    "last_reviewed": n.last_reviewed,
                    "encounter_count": n.encounter_count,
                    "mastery_score": n.mastery_score,
                    "struggle_count": n.struggle_count,
                    "quiz_attempts": n.quiz_attempts,
                }
                for concept, n in self.nodes.items()
            },
            "summary": self.summary(),
        }


# ---------------------------------------------------------------------------
# Graph construction
# ---------------------------------------------------------------------------


def build_knowledge_graph(
    composition: CompositionService,
    student_id: str,
    classroom_id: str | None = None,
    course_id: str | None = None,
) -> KnowledgeGraph:
    """Walk the student's fragments and assemble a knowledge graph."""
    graph = KnowledgeGraph(
        student_id=student_id,
        classroom_id=classroom_id,
        course_id=course_id,
    )

    for artifact in composition.artifact_registry.list(kind="fragment"):
        data = artifact.data
        content = data.get("content", {})

        # Only consider fragments for this student
        prov_principal = data.get("provenance", {}).get("principal_id")
        content_student = content.get("student_id")
        if prov_principal != student_id and content_student != student_id:
            continue

        ct = data.get("cognitive_type")
        # Prefer content.event_time (the semantic event time) over
        # provenance.timestamp (the ingestion time). For episodic
        # fragments these differ — event_time is what we care about.
        ts = (
            content.get("event_time")
            or data.get("provenance", {}).get("timestamp")
        )

        # 1. Episodic traces contribute concepts via topics[]
        if ct == "episodic":
            for topic in content.get("topics", []):
                _touch(graph, topic, timestamp=ts)

        # 2. Semantic (quiz scores) — attach mastery via final_score
        if ct == "semantic":
            topic = content.get("topic")
            if topic:
                node = _touch(graph, topic, timestamp=ts)
                node.quiz_attempts += 1
                fs = content.get("final_score")
                if fs is not None:
                    if node.mastery_score is None or fs > node.mastery_score:
                        node.mastery_score = fs

        # 3. Signals that indicate struggle — stuck / misconception
        # (checked independently — signal fragments can be any cognitive_type)
        if content.get("signal_type") in (
            "student_stuck", "misconception_detected"
        ):
            topic = content.get("topic") or content.get("misconception_id")
            if topic:
                node = _touch(graph, topic, timestamp=ts)
                node.struggle_count += 1

        # 4. Help tickets — treat as struggle signal
        if content.get("ticket_id"):
            topic = content.get("topic")
            if topic:
                node = _touch(graph, topic, timestamp=ts)
                node.struggle_count += 1

    return graph


def _touch(
    graph: KnowledgeGraph, concept: str, timestamp: str | None = None
) -> ConceptNode:
    """Get-or-create a concept node and update its encounter metadata."""
    node = graph.nodes.get(concept)
    if node is None:
        node = ConceptNode(concept=concept)
        graph.nodes[concept] = node
    node.encounter_count += 1
    if timestamp:
        if node.first_encountered is None or timestamp < node.first_encountered:
            node.first_encountered = timestamp
        if node.last_reviewed is None or timestamp > node.last_reviewed:
            node.last_reviewed = timestamp
    return node
