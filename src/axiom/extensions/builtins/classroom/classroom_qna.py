# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Grounded-answer synthesis over classroom citations.

Tier B piece 3. Layers an LLM step on top of the citation retrieval
shipped in Phase 6 of the materials-flow tier. Given a question and
the top-k citations from the student's local classroom index, the
engine produces a synthesized answer that references the source
titles.

Graceful fallback: the LLM callable is allowed to return None or
raise — either way the engine returns citations-only rather than
propagating the failure. This preserves the "works offline" property
of `axi classroom ask`, which was a hard requirement from the
materials-flow tier: students on planes, students during a coordinator-host
outage, and students without any LLM key at all should still get
useful output.

Wire-up at the CLI layer: call ``Gateway.complete(prompt, system)``
and pass a ``llm`` adapter that returns ``resp.text`` on success or
``None`` when ``resp.success`` is False.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Citation:
    """One retrieved passage from the student's local class index."""

    title: str
    text: str
    file_id: str


@dataclass(frozen=True)
class QAResponse:
    """Result of a Q&A run."""

    answer: str
    cited_titles: list[str]
    success: bool
    error: str | None = None


# Shape: callable takes a user prompt + optional system prompt, returns
# the LLM's text response or None if no provider was available. Exceptions
# are caught by the engine — callers don't need to handle them here.
LLMFn = Callable[..., str | None]


# ---------------------------------------------------------------------------
# Prompt assembly
# ---------------------------------------------------------------------------


_SYSTEM_PROMPT = (
    "You are a grounded tutor answering questions using only the passages "
    "provided. Rules:\n"
    "1. Answer ONLY from the passages. If the passages don't contain the "
    "answer, say so plainly — do not draw on outside knowledge.\n"
    "2. Cite the source by its title in square brackets after the relevant "
    "claim, e.g. '...absorbs neutrons [Chapter 1 — Control rods]'.\n"
    "3. Keep the answer concise. No preamble like 'Based on the passages'."
)


def _build_prompt(question: str, citations: list[Citation]) -> str:
    parts = ["Passages:\n"]
    for i, c in enumerate(citations, 1):
        parts.append(f"[{i}] {c.title}")
        parts.append(c.text.strip())
        parts.append("")
    parts.append(f"Question: {question}")
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------


def answer_question(
    *,
    question: str,
    citations: list[Citation],
    llm: LLMFn,
) -> QAResponse:
    """Synthesize a grounded answer, or fall back to citations-only.

    Returns a :class:`QAResponse`:
      - ``answer`` is a non-empty synthesized string when the LLM
        produced one; otherwise empty.
      - ``cited_titles`` is the ordered list of citation titles passed
        in (always present, even on fallback).
      - ``success`` is True iff we have something useful to show the
        student — answer OR citations. False only when we have neither
        (empty citations list).
      - ``error`` is set when an LLM exception was suppressed, so the
        CLI can optionally narrate "offline mode" context.
    """
    cited_titles = [c.title for c in citations]

    if not citations:
        return QAResponse(
            answer="",
            cited_titles=[],
            success=False,
        )

    prompt = _build_prompt(question, citations)

    # LLM is allowed to fail gracefully — either by returning None or
    # by raising. Both paths fall through to citations-only.
    try:
        text = llm(prompt, system=_SYSTEM_PROMPT)
    except Exception as exc:  # noqa: BLE001 — citations-only is our floor
        return QAResponse(
            answer="",
            cited_titles=cited_titles,
            success=True,
            error=f"LLM unavailable: {exc}",
        )

    if not text:
        return QAResponse(
            answer="",
            cited_titles=cited_titles,
            success=True,
        )

    return QAResponse(
        answer=text.strip(),
        cited_titles=cited_titles,
        success=True,
    )


__all__ = [
    "Citation",
    "LLMFn",
    "QAResponse",
    "answer_question",
]
