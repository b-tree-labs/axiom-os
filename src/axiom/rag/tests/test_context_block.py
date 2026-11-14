# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""TDD tests for the T0-1 ``rag_context_block`` builder.

Produces the structured context passage the model sees. Must be stable
across calls (so the model learns one format) and lossless enough that
the citation postprocessor can verify any [C<n>] marker deterministically.
"""

from __future__ import annotations

from axiom.rag.context_block import build_rag_context_block
from axiom.rag.retriever import RetrievedChunk


def _ch(key: str, path: str, text: str, idx: int = 0, title: str | None = None) -> RetrievedChunk:
    return RetrievedChunk(
        citation_key=key,
        rank=int(key[1:]),
        source_path=path,
        source_title=title or path,
        chunk_text=text,
        chunk_index=idx,
        corpus="rag-internal",
        similarity=0.5,
        rrf_score=0.03,
    )


class TestBlockShape:
    def test_empty_retrieved_returns_empty(self):
        assert build_rag_context_block([]) == ""

    def test_block_carries_header(self):
        out = build_rag_context_block([_ch("C1", "a.md", "hello")])
        assert "Retrieved Context" in out
        assert "[C1]" in out

    def test_every_chunk_gets_citation_marker_and_text(self):
        out = build_rag_context_block([
            _ch("C1", "a.md", "alpha"),
            _ch("C2", "b.md", "beta"),
        ])
        assert "[C1]" in out and "alpha" in out
        assert "[C2]" in out and "beta" in out

    def test_source_path_rendered(self):
        out = build_rag_context_block([_ch("C1", "path/doc.md", "hi", title="Doc")])
        assert "path/doc.md" in out


class TestGuidance:
    def test_instructs_model_to_cite_inline(self):
        """The guidance text must tell the model to cite with [C<n>]."""
        out = build_rag_context_block([_ch("C1", "a.md", "x")])
        assert "[C" in out
        assert "cite" in out.lower() or "citation" in out.lower()

    def test_instructs_model_not_to_fabricate(self):
        """Explicit 'don't invent' guidance to cut hallucinated sources."""
        out = build_rag_context_block([_ch("C1", "a.md", "x")])
        lowered = out.lower()
        assert "invent" in lowered or "fabricate" in lowered or "do not cite" in lowered


class TestStability:
    def test_identical_input_identical_output(self):
        rs = [_ch("C1", "a.md", "alpha"), _ch("C2", "b.md", "beta")]
        assert build_rag_context_block(rs) == build_rag_context_block(rs)

    def test_order_preserved(self):
        out = build_rag_context_block([
            _ch("C1", "a.md", "alpha"),
            _ch("C2", "b.md", "beta"),
        ])
        assert out.index("alpha") < out.index("beta")


class TestAccessMetadata:
    def test_non_public_tier_rendered(self):
        ch = RetrievedChunk(
            citation_key="C1",
            rank=1,
            source_path="a.md",
            source_title="A",
            chunk_text="secret",
            chunk_index=0,
            corpus="rag-org",
            similarity=0.5,
            rrf_score=0.03,
            access_tier="institutional",
        )
        out = build_rag_context_block([ch])
        assert "institutional" in out

    def test_public_tier_not_rendered(self):
        """Default 'public' tier is omitted to reduce prompt bloat."""
        out = build_rag_context_block([_ch("C1", "a.md", "x")])
        assert "public" not in out.lower().replace("the public", "")
