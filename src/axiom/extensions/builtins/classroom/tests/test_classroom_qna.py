# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tests for the classroom Q&A engine — grounded-answer synthesis.

Layers an LLM step on top of the citation retrieval already shipped
in Phase 6. Given a question and the top-k citations, the engine
produces a synthesized answer that references the source titles.

Graceful fallback is a first-class requirement: when no LLM provider
is available (airplane mode, instructor's coordinator is offline, no
OPENAI_API_KEY), the engine must return citations-only rather than
raise — this is consistent with the rest of Axiom's provider-chain
philosophy and with the `ask` command's existing behavior.
"""

from __future__ import annotations

import pytest

from axiom.extensions.builtins.classroom.classroom_qna import (
    Citation,
    QAResponse,
    answer_question,
)

# ---------------------------------------------------------------------------
# Helpers — stub LLMs
# ---------------------------------------------------------------------------


def _stub_llm_echo(prompt: str, *, system: str = "") -> str:
    """Returns a predictable string that proves the prompt was assembled."""
    return f"[stub answer] prompt-hash={hash(prompt) % 10000}"


def _stub_llm_cites(prompt: str, *, system: str = "") -> str:
    """Returns an answer that pretends to cite two sources."""
    return "A control rod [Chapter 1] absorbs neutrons [Chapter 2]."


def _stub_llm_none(prompt: str, *, system: str = "") -> str | None:
    """Simulates an unavailable LLM — returns None, engine must fall back."""
    return None


SAMPLE_CITATIONS = [
    Citation(
        title="Chapter 1 — Control rods",
        text="Control rods absorb neutrons to slow fission reactions.",
        file_id="f1",
    ),
    Citation(
        title="Chapter 2 — Materials",
        text="Boron and cadmium are commonly used in control rod construction.",
        file_id="f2",
    ),
]


# ---------------------------------------------------------------------------
# Happy path — LLM available
# ---------------------------------------------------------------------------


class TestAnswerWithLLM:
    def test_returns_llm_text_when_llm_available(self):
        r = answer_question(
            question="What is a control rod?",
            citations=SAMPLE_CITATIONS,
            llm=_stub_llm_cites,
        )
        assert r.success is True
        assert r.answer == "A control rod [Chapter 1] absorbs neutrons [Chapter 2]."
        assert r.cited_titles == ["Chapter 1 — Control rods", "Chapter 2 — Materials"]

    def test_prompt_includes_question_and_citations(self):
        captured: dict = {}

        def capture(prompt: str, *, system: str = "") -> str:
            captured["prompt"] = prompt
            captured["system"] = system
            return "ok"

        answer_question(
            question="What is a control rod?",
            citations=SAMPLE_CITATIONS,
            llm=capture,
        )
        assert "What is a control rod?" in captured["prompt"]
        # Both citation texts appear in the prompt.
        assert "absorb neutrons" in captured["prompt"]
        assert "Boron and cadmium" in captured["prompt"]
        # Titles are visible so the LLM can cite by name.
        assert "Chapter 1 — Control rods" in captured["prompt"]
        assert "Chapter 2 — Materials" in captured["prompt"]

    def test_system_prompt_instructs_grounding(self):
        captured: dict = {}

        def capture(prompt: str, *, system: str = "") -> str:
            captured["system"] = system
            return "ok"

        answer_question(
            question="q",
            citations=SAMPLE_CITATIONS,
            llm=capture,
        )
        system = captured["system"].lower()
        # Grounding instruction: don't answer from world knowledge.
        assert "only" in system or "passages" in system or "cited" in system
        # Citation instruction.
        assert "cite" in system or "source" in system or "title" in system


# ---------------------------------------------------------------------------
# Fallback paths
# ---------------------------------------------------------------------------


class TestFallback:
    def test_no_llm_available_returns_success_with_citations_only(self):
        r = answer_question(
            question="q",
            citations=SAMPLE_CITATIONS,
            llm=_stub_llm_none,
        )
        # success=True because the engine did useful work: returning
        # grounded citations is a valid result, even without synthesis.
        assert r.success is True
        # answer is empty / None so callers can tell it's not synthesized.
        assert not r.answer
        # Citations still reported.
        assert r.cited_titles == ["Chapter 1 — Control rods", "Chapter 2 — Materials"]

    def test_empty_citations_returns_no_match(self):
        r = answer_question(
            question="q",
            citations=[],
            llm=_stub_llm_cites,
        )
        # Without citations, engine should not call the LLM — avoids
        # confabulation from thin context.
        assert r.success is False
        assert r.answer == ""
        assert r.cited_titles == []

    def test_llm_raising_is_caught(self):
        def angry(prompt: str, *, system: str = "") -> str:
            raise RuntimeError("provider exploded")

        r = answer_question(
            question="q",
            citations=SAMPLE_CITATIONS,
            llm=angry,
        )
        # Fall back to citations-only, don't propagate.
        assert r.success is True
        assert not r.answer
        assert r.cited_titles == ["Chapter 1 — Control rods", "Chapter 2 — Materials"]


# ---------------------------------------------------------------------------
# QAResponse shape
# ---------------------------------------------------------------------------


class TestQAResponseShape:
    def test_qa_response_is_immutable(self):
        r = QAResponse(
            answer="a", cited_titles=["t"], success=True,
        )
        with pytest.raises((AttributeError, Exception)):
            r.answer = "b"  # frozen dataclass

    def test_citation_is_immutable(self):
        c = Citation(title="t", text="x", file_id="f")
        with pytest.raises((AttributeError, Exception)):
            c.title = "u"
