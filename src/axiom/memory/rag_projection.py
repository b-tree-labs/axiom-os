# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0

"""Memory → RAG projection (ADR-069 §2-3) — the load-bearing boundary.

ADR-069 makes memory the source of truth and RAG a unified retrieval layer
fed by (i) external ingested docs and (ii) a *derived projection of
retrievable memory fragments*. This module is feeder (ii): the pure
policy + transform that decides which fragments become RAG chunks and
shapes them, carrying `cognitive_type` + `fragment_ref` so provenance
survives into retrieval.

Privacy/correctness floors, enforced by construction (not policy):

- **Only `semantic` projects** — semantic fragments ARE the matured facts
  (maturation distills episodic → semantic). `resource` projection is an
  open question (ADR-069 Q2), deferred.
- **`vault`, raw `episodic`, `core` are NEVER projected.** `vault` is
  secret; raw `episodic`/`core` are pre-maturation. Retrieval can't leak
  them because the projector refuses to emit them.
- **`procedural` does NOT go to RAG** — it graduates into Skills (ADR-069
  §5 / self-improvement epic), not the knowledge index.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from axiom.memory.fragment import CognitiveType, MemoryFragment

# Only matured semantic facts are retrievable knowledge today.
_PROJECTABLE: frozenset[CognitiveType] = frozenset({CognitiveType.SEMANTIC})


def should_project(fragment: MemoryFragment) -> bool:
    """True iff this fragment may be projected into RAG (semantic only)."""
    return fragment.cognitive_type in _PROJECTABLE


@dataclass(frozen=True)
class ProjectedChunk:
    """A RAG chunk derived from a memory fragment (feeder ii).

    ``fragment_ref`` back-references the source fragment so retrieval keeps
    provenance and supersession can invalidate the chunk (ADR-069 Q3).
    ``principal_id`` + ``agents`` carry the `(agent, owner)` scope so
    cross-channel recall is a scoped query, channel-independent.
    """

    chunk_text: str
    cognitive_type: str
    fragment_ref: str
    principal_id: str
    agents: tuple[str, ...] = ()
    corpus: str = "rag-internal"
    source_type: str = "memory"


def _render_text(content: dict) -> str:
    """Best-effort human-readable text for a semantic fragment."""
    for key in ("summary", "text", "fact", "statement"):
        val = content.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()
    return json.dumps(content, sort_keys=True)


def fragment_to_chunk(
    fragment: MemoryFragment, *, corpus: str = "rag-internal"
) -> ProjectedChunk:
    """Project one fragment into a :class:`ProjectedChunk`.

    Raises :class:`ValueError` if the fragment is not projectable — the
    never-project floor holds even against a direct call.
    """
    if not should_project(fragment):
        raise ValueError(
            f"refusing to project {fragment.cognitive_type.value} fragment "
            f"{fragment.id} — only semantic fragments project (ADR-069)"
        )
    prov = fragment.provenance
    return ProjectedChunk(
        chunk_text=_render_text(fragment.content),
        cognitive_type=fragment.cognitive_type.value,
        fragment_ref=fragment.id,
        principal_id=prov.principal_id,
        agents=tuple(sorted(prov.agents)),
        corpus=corpus,
    )


def project_fragments(
    fragments: list[MemoryFragment], *, corpus: str = "rag-internal"
) -> list[ProjectedChunk]:
    """Project every projectable fragment; silently skip the rest."""
    return [
        fragment_to_chunk(f, corpus=corpus) for f in fragments if should_project(f)
    ]


__all__ = [
    "should_project",
    "ProjectedChunk",
    "fragment_to_chunk",
    "project_fragments",
]
