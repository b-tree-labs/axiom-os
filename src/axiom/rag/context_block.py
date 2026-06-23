# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Build the ``rag_context_block`` injected into the chat system prompt.

Responsibilities:
    - Render retrieved chunks with stable ``[C<n>]`` markers so the
      citation postprocessor can verify the model's inline citations.
    - Give the model explicit guidance: cite what you use, do not invent
      sources, prefer "I don't know" over fabrication.
    - Surface non-default access metadata (tier other than ``public``,
      classification other than ``unclassified``) so the model can
      reason about sensitivity when drafting.

The block is assembled at call time because the chunk list is dynamic.
A future iteration may split into a cached static preamble and a
dynamic chunk body for Anthropic ephemeral cache wins — skipped in v1
to keep the wiring simple.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

from axiom.rag.retriever import RetrievedChunk

# Strip control characters that break downstream chat-template processing.
# OCR'd PDF chunks commonly contain form-feed (\x0c) and other non-printable
# bytes that some LLM backends reject with a 400, even though the bytes are
# valid JSON-escaped on the wire. Keep \t, \n, \r — they carry structure.
_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def _sanitize_chunk_text(text: str) -> str:
    return _CONTROL_CHARS.sub("", text)


_HEADER = "## Retrieved Context"
_GUIDANCE = (
    "Use the passages below to ground your answer. Cite each factual claim "
    "inline using the marker in square brackets (e.g., [C1] or [C1, C2]). "
    "Do not invent sources: if none of the passages answer the question, "
    "say so explicitly rather than citing a marker that is not listed."
)
_FOOTER = "End of retrieved context."


def build_rag_context_block(retrieved: Iterable[RetrievedChunk]) -> str:
    """Render the context block the model sees. Empty input → empty string."""
    chunks = list(retrieved)
    if not chunks:
        return ""

    lines: list[str] = [_HEADER, "", _GUIDANCE, ""]
    for ch in chunks:
        meta_parts = [f"source: {ch.source_path}"]
        if ch.corpus:
            meta_parts.append(f"corpus: {ch.corpus}")
        if ch.source_title and ch.source_title != ch.source_path:
            meta_parts.append(f"title: {ch.source_title}")
        if ch.access_tier != "public":
            meta_parts.append(f"tier: {ch.access_tier}")
        if ch.classification != "unclassified":
            meta_parts.append(f"classification: {ch.classification}")
        meta = " · ".join(meta_parts)
        lines.append(f"[{ch.citation_key}] ({meta})")
        lines.append(_sanitize_chunk_text(ch.chunk_text).rstrip())
        lines.append("")
    lines.append(_FOOTER)
    return "\n".join(lines)
