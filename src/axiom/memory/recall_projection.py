# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0

"""Memory → rag-memory recall projection (ADR-088).

The per-principal ``rag-memory`` corpus is the retrieval index for
``CompositionService.recall()`` — semantic search over a user's *own*
memory. It sits beside the document corpora (ADR-069 governs those,
unchanged): corpus name ``rag-memory:<principal>``, never blended into
document-corpus queries unless a plan explicitly requests fusion.

Type policy, enforced by construction (no configuration surface):

- ``core``, ``semantic``, ``episodic``, ``procedural``, ``resource``
  project by default.
- **``vault`` is categorically excluded.** The projector refuses a
  direct call and skips it in bulk; there is no parameter that can
  widen the policy.
- ``resource`` projects as metadata + pointer (``ref`` + descriptive
  fields), never blob content (ADR-069 open question 2, answered for
  this corpus).

Every chunk carries ``cognitive_type``, ``fragment_ref``,
``visibility``, ``classification`` (ADR-088 §5) so downstream gates can
pre-filter, and projects under ``source_path = memory://<fragment id>``
so per-fragment eviction is the store's normal per-path delete. The
projection is a rebuildable index with no write path back into the
ledger: delete-and-rebuild is always safe.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from axiom.memory.fragment import CognitiveType, MemoryFragment
from axiom.rag.chunker import Chunk

RECALL_CORPUS_PREFIX = "rag-memory"

# ADR-088 §2 — vault is absent by construction; everything else recalls.
RECALL_PROJECTABLE: frozenset[CognitiveType] = frozenset(
    {
        CognitiveType.CORE,
        CognitiveType.SEMANTIC,
        CognitiveType.EPISODIC,
        CognitiveType.PROCEDURAL,
        CognitiveType.RESOURCE,
    }
)

# Resource fragments render ONLY these keys — pointer + descriptive
# metadata. Anything else in the content dict is treated as blob-ish
# and never enters the index.
_RESOURCE_RENDER_KEYS = ("ref", "name", "title", "description", "mime_type")

_TEXT_KEYS = ("summary", "text", "fact", "statement", "note", "persona")


def recall_corpus_for(principal_id: str) -> str:
    """Corpus name convention: ``rag-memory:<principal>``."""
    return f"{RECALL_CORPUS_PREFIX}:{principal_id}"


def should_recall_project(fragment: MemoryFragment) -> bool:
    """True iff this fragment may enter the rag-memory corpus."""
    return fragment.cognitive_type in RECALL_PROJECTABLE


@dataclass(frozen=True)
class RecallChunk:
    """One fragment's entry in the rag-memory corpus (ADR-088 §5).

    ``fragment_ref`` ties the chunk back to its ledger fragment for
    provenance and eviction; ``visibility``/``classification`` ride
    along so retrieval-time pre-filters and the (P3) serving gate never
    need to re-open the ledger to decide.
    """

    chunk_text: str
    cognitive_type: str
    fragment_ref: str
    principal_id: str
    visibility: str
    classification: dict
    corpus: str
    event_time: str | None = None

    @property
    def source_path(self) -> str:
        return f"memory://{self.fragment_ref}"

    def to_rag_chunk(self) -> Chunk:
        """Map onto the RAG store's chunk shape (one chunk per fragment)."""
        return Chunk(
            text=self.chunk_text,
            source_path=self.source_path,
            source_title=self.cognitive_type,
            chunk_index=0,
            start_line=0,
            source_type="memory",
        )


def _render_resource(content: dict) -> str:
    parts = [
        f"{key}: {content[key]}"
        for key in _RESOURCE_RENDER_KEYS
        if isinstance(content.get(key), str) and content[key].strip()
    ]
    return "\n".join(parts)


def _render_text(cognitive_type: CognitiveType, content: dict) -> str:
    """Best-effort recallable text per cognitive type."""
    if cognitive_type is CognitiveType.RESOURCE:
        return _render_resource(content)
    parts: list[str] = []
    for key in _TEXT_KEYS:
        val = content.get(key)
        if isinstance(val, str) and val.strip():
            parts.append(val.strip())
    if cognitive_type is CognitiveType.PROCEDURAL:
        steps = content.get("steps")
        if isinstance(steps, list):
            parts.extend(str(s) for s in steps)
    if parts:
        return "\n".join(parts)
    return json.dumps(content, sort_keys=True)


def fragment_to_recall_chunk(fragment: MemoryFragment) -> RecallChunk:
    """Project one fragment into the rag-memory corpus shape.

    Raises :class:`ValueError` for ``vault`` — the never-project floor
    holds even against a direct call.
    """
    if not should_recall_project(fragment):
        raise ValueError(
            f"refusing to project {fragment.cognitive_type.value} fragment "
            f"{fragment.id} into rag-memory — vault never projects (ADR-088)"
        )
    prov = fragment.provenance
    event_time = fragment.content.get("event_time")
    return RecallChunk(
        chunk_text=_render_text(fragment.cognitive_type, fragment.content),
        cognitive_type=fragment.cognitive_type.value,
        fragment_ref=fragment.id,
        principal_id=prov.principal_id,
        visibility=fragment.visibility.value,
        classification=fragment.classification.to_dict(),
        corpus=recall_corpus_for(prov.principal_id),
        event_time=event_time if isinstance(event_time, str) else None,
    )


def project_for_recall(fragments: list[MemoryFragment]) -> list[RecallChunk]:
    """Project every recallable fragment; skip the rest (vault included)."""
    return [
        fragment_to_recall_chunk(f)
        for f in fragments
        if should_recall_project(f)
    ]


__all__ = [
    "RECALL_CORPUS_PREFIX",
    "RECALL_PROJECTABLE",
    "RecallChunk",
    "fragment_to_recall_chunk",
    "project_for_recall",
    "recall_corpus_for",
    "should_recall_project",
]
