# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Regression test for chunk-text sanitization in the RAG context block.

The bug this guards against (encountered 2026-05-22 on the live gateway):

OCR'd PDF chunks indexed from documents like ``10CFR-Part50-Domestic-Licensing.pdf``
contain control characters such as form feed (\\x0c), separators, and
non-printable bytes. When these get inlined into the system prompt and
forwarded to a downstream LLM that applies a Jinja chat template
(llama-server with ``--jinja``), the request fails with HTTP 400 — and
the failure is non-obvious because:

  - The bare LLM endpoint accepts identical-length prompts without
    control chars and returns 200 in 3 seconds.
  - The gateway path with ``?raw=1`` (bypass RAG) also returns 200.
  - Only the RAG-augmented path fails, and only on prompts whose
    top-K retrieval includes one of the problematic chunks.

So the failure is correlated with corpus content, not query shape. Unit
tests that build the context block from synthetic chunks won't catch it
unless they explicitly include the problem characters.
"""

from __future__ import annotations

from dataclasses import dataclass

from axiom.rag.context_block import _sanitize_chunk_text, build_rag_context_block


@dataclass
class _FakeChunk:
    """Stand-in for RetrievedChunk — only the fields context_block reads."""

    chunk_text: str
    source_path: str = "test.md"
    source_title: str = "Test"
    corpus: str = "rag-org"
    access_tier: str = "public"
    classification: str = "unclassified"
    citation_key: str = "C1"


class TestSanitizeChunkText:
    def test_strips_form_feed(self):
        assert "\x0c" not in _sanitize_chunk_text("page1\x0cpage2")

    def test_strips_other_control_chars(self):
        dirty = "".join(chr(c) for c in (0, 1, 7, 8, 11, 14, 16, 27, 31, 127))
        assert _sanitize_chunk_text(dirty) == ""

    def test_preserves_tab_newline_carriage_return(self):
        clean = "line1\nline2\tindented\rreturn"
        assert _sanitize_chunk_text(clean) == clean

    def test_preserves_normal_unicode(self):
        unicode_text = "°C ± 5% with λ=1064nm and é accents"
        assert _sanitize_chunk_text(unicode_text) == unicode_text

    def test_preserves_latex_style_math(self):
        # Common in OCR'd nuclear-engineering docs; not a control char,
        # should pass through untouched.
        latex = r"The $^{233}\mathrm{U}$ concentration was 0.13 mole %"
        assert _sanitize_chunk_text(latex) == latex


class TestContextBlockSanitization:
    """Integration check: context-block output never contains control
    characters in the chunk-text region, even when chunks do."""

    def test_form_feed_in_chunk_does_not_reach_output(self):
        chunk = _FakeChunk(
            chunk_text="Section 1.\x0cSection 2 starts after a page break.",
            source_path="10CFR-Part50.pdf",
            citation_key="C1",
        )
        block = build_rag_context_block([chunk])
        assert "\x0c" not in block
        assert "Section 1.Section 2 starts after a page break." in block

    def test_mixed_clean_and_dirty_chunks_both_render(self):
        chunks = [
            _FakeChunk(chunk_text="clean content", citation_key="C1"),
            _FakeChunk(
                chunk_text="dirty\x0ccontent with\x07bell",
                citation_key="C2",
            ),
        ]
        block = build_rag_context_block(chunks)
        assert "[C1]" in block
        assert "[C2]" in block
        assert "\x0c" not in block
        assert "\x07" not in block
        assert "clean content" in block
        assert "dirtycontent withbell" in block

    def test_structural_whitespace_inside_chunk_is_preserved(self):
        chunk = _FakeChunk(chunk_text="line1\n\nline2\tcol2", citation_key="C1")
        block = build_rag_context_block([chunk])
        assert "line1\n\nline2\tcol2" in block
