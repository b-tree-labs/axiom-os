# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Citation postprocessor — deterministic verification of [C<n>] markers.

Runs after the model's response is complete. Extracts every ``[C<n>]``
marker in the text, verifies each corresponds to a retrieved chunk,
emits a structured ``CitationEnvelope`` the UI (and audit log) render
against. Unresolved markers are surfaced rather than silently stripped;
an optional ``strict=True`` mode raises so the caller can fail closed.

Grammar supported (deliberately narrow):
    [C1]
    [C1, C2]
    [C1,C2,C3]    (any amount of whitespace around commas)

Range syntax like ``[C1-C3]`` is not supported — it becomes ambiguous if
the range exceeds the retrieved set, and frontier providers uniformly
emit comma lists.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass, field

from axiom.rag.retriever import RetrievedChunk

_MARKER_RE = re.compile(r"\[(C\d+(?:\s*,\s*C\d+)*)\]")


@dataclass(frozen=True)
class CitationReference:
    """One cited source, with provenance pulled from its RetrievedChunk."""

    citation_key: str
    source_path: str
    source_title: str
    chunk_index: int
    corpus: str
    mention_count: int = 1

    def to_dict(self) -> dict:
        return {
            "citation_key": self.citation_key,
            "source_path": self.source_path,
            "source_title": self.source_title,
            "chunk_index": self.chunk_index,
            "corpus": self.corpus,
            "mention_count": self.mention_count,
        }


@dataclass(frozen=True)
class CitationEnvelope:
    """Structured view of a model response after citation verification."""

    text: str
    cited: list[CitationReference]
    unresolved: list[str] = field(default_factory=list)
    unused: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "text": self.text,
            "cited": [c.to_dict() for c in self.cited],
            "unresolved": list(self.unresolved),
            "unused": list(self.unused),
        }


def postprocess_citations(
    text: str,
    retrieved: Iterable[RetrievedChunk],
    strict: bool = False,
) -> CitationEnvelope:
    """Extract, verify, and structure the citations in ``text``.

    Args:
        text: Raw model output.
        retrieved: The chunks the retriever fed to the model. Must carry
            stable ``citation_key`` values (C1, C2, ...).
        strict: If True, raise ``ValueError`` when any marker is
            unresolved. Default False — unresolved markers land in
            ``envelope.unresolved`` for the caller to inspect.

    Returns:
        A ``CitationEnvelope`` with ``cited`` (verified), ``unresolved``
        (referenced but absent from retrieved), and ``unused``
        (retrieved but never cited).
    """
    retrieved_list = list(retrieved)
    retrieved_by_key: dict[str, RetrievedChunk] = {
        r.citation_key: r for r in retrieved_list
    }

    mentions: dict[str, int] = {}
    unresolved: list[str] = []
    seen_unresolved: set[str] = set()

    for match in _MARKER_RE.finditer(text):
        group = match.group(1)
        for token in (t.strip() for t in group.split(",")):
            if token in retrieved_by_key:
                mentions[token] = mentions.get(token, 0) + 1
            elif token not in seen_unresolved:
                unresolved.append(token)
                seen_unresolved.add(token)

    if strict and unresolved:
        raise ValueError(
            f"Unresolved citation markers in model output: {unresolved}"
        )

    cited: list[CitationReference] = []
    for key, count in mentions.items():
        chunk = retrieved_by_key[key]
        cited.append(
            CitationReference(
                citation_key=key,
                source_path=chunk.source_path,
                source_title=chunk.source_title,
                chunk_index=chunk.chunk_index,
                corpus=chunk.corpus,
                mention_count=count,
            )
        )

    cited_keys = {c.citation_key for c in cited}
    unused = [r.citation_key for r in retrieved_list if r.citation_key not in cited_keys]

    return CitationEnvelope(
        text=text,
        cited=cited,
        unresolved=unresolved,
        unused=unused,
    )
