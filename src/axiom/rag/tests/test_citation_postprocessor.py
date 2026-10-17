# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""TDD tests for the T0-1 citation postprocessor.

Runs *after* the model emits its response. Deterministic verification:

    - Every [C<n>] marker in the text must correspond to a retrieved chunk.
      Unresolved markers are surfaced (not silently stripped) so the
      caller can decide: retry, annotate, or fail closed.
    - The resulting ``CitationEnvelope`` lets the UI render a structured
      list of citations alongside the narrative text.

Markers may be single (``[C1]``) or multi (``[C1, C2]`` / ``[C1,C3]``).
Range syntax (``[C1-C3]``) is deliberately *not* supported in v1 — makes
the verifier ambiguous if the range exceeds the retrieved set.
"""

from __future__ import annotations

import pytest

from axiom.rag.citation import (
    CitationEnvelope,
    CitationReference,
    postprocess_citations,
)
from axiom.rag.retriever import RetrievedChunk

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _ch(key: str, path: str, idx: int = 0, title: str | None = None) -> RetrievedChunk:
    return RetrievedChunk(
        citation_key=key,
        rank=int(key[1:]),
        source_path=path,
        source_title=title or path,
        chunk_text="...",
        chunk_index=idx,
        corpus="rag-internal",
        similarity=0.5,
        rrf_score=0.03,
    )


# ---------------------------------------------------------------------------
# Basic extraction
# ---------------------------------------------------------------------------


class TestExtractCitations:
    def test_single_marker(self):
        text = "Neutron scattering is well-understood [C1]."
        retrieved = [_ch("C1", "scatter.md")]
        env = postprocess_citations(text, retrieved)
        assert isinstance(env, CitationEnvelope)
        assert [c.citation_key for c in env.cited] == ["C1"]
        assert env.unresolved == []

    def test_multi_cite_single_bracket(self):
        text = "Both topics converge [C1, C2]."
        retrieved = [_ch("C1", "a.md"), _ch("C2", "b.md")]
        env = postprocess_citations(text, retrieved)
        assert {c.citation_key for c in env.cited} == {"C1", "C2"}

    def test_multi_cite_no_spaces(self):
        text = "Both converge [C1,C2,C3]."
        retrieved = [_ch("C1", "a.md"), _ch("C2", "b.md"), _ch("C3", "c.md")]
        env = postprocess_citations(text, retrieved)
        assert {c.citation_key for c in env.cited} == {"C1", "C2", "C3"}

    def test_no_markers_returns_empty_cited(self):
        text = "No citations here."
        retrieved = [_ch("C1", "a.md")]
        env = postprocess_citations(text, retrieved)
        assert env.cited == []


# ---------------------------------------------------------------------------
# Deterministic verification
# ---------------------------------------------------------------------------


class TestVerification:
    def test_unresolved_marker_surfaced(self):
        """A marker in text but absent from retrieved = unresolved."""
        text = "Claim [C1] and also [C9]."
        retrieved = [_ch("C1", "a.md")]
        env = postprocess_citations(text, retrieved)
        assert env.unresolved == ["C9"]
        assert [c.citation_key for c in env.cited] == ["C1"]

    def test_unused_retrieved_reported(self):
        """Chunks retrieved but not cited end up in ``unused``."""
        text = "Only cite [C1]."
        retrieved = [_ch("C1", "a.md"), _ch("C2", "b.md")]
        env = postprocess_citations(text, retrieved)
        assert env.unused == ["C2"]

    def test_citation_reference_carries_source_metadata(self):
        text = "Statement [C1]."
        retrieved = [_ch("C1", "path/to/doc.md", idx=5, title="Doc Title")]
        env = postprocess_citations(text, retrieved)
        ref = env.cited[0]
        assert isinstance(ref, CitationReference)
        assert ref.source_path == "path/to/doc.md"
        assert ref.source_title == "Doc Title"
        assert ref.chunk_index == 5


class TestDuplicateMarkers:
    def test_same_marker_twice_cited_once(self):
        text = "[C1] is mentioned here and also here [C1]."
        retrieved = [_ch("C1", "a.md")]
        env = postprocess_citations(text, retrieved)
        assert len(env.cited) == 1
        assert env.cited[0].mention_count == 2


class TestStrictMode:
    def test_strict_raises_on_unresolved(self):
        text = "[C1] and [C99]."
        retrieved = [_ch("C1", "a.md")]
        with pytest.raises(ValueError, match="C99"):
            postprocess_citations(text, retrieved, strict=True)

    def test_strict_ok_when_all_resolved(self):
        text = "[C1]."
        retrieved = [_ch("C1", "a.md")]
        env = postprocess_citations(text, retrieved, strict=True)
        assert env.unresolved == []


# ---------------------------------------------------------------------------
# Envelope rendering helpers
# ---------------------------------------------------------------------------


class TestEnvelopeRendering:
    def test_to_dict_shape(self):
        text = "[C1]"
        retrieved = [_ch("C1", "a.md", title="A")]
        env = postprocess_citations(text, retrieved)
        d = env.to_dict()
        assert d["text"] == "[C1]"
        assert d["cited"][0]["citation_key"] == "C1"
        assert d["cited"][0]["source_path"] == "a.md"
        assert d["unresolved"] == []
        assert "unused" in d
