# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""LLM-backed summarizer for dropped conversation history (#83).

T0-2's :mod:`axiom.infra.conversation_window` already accepts a
pluggable summarizer callable. This module builds a concrete LLM-
backed one using :func:`axiom.infra.structured_output.structured_output`
so the emitted summary is schema-validated — no regex parsing of free-
form model output.

Failure modes fall back to the deterministic summarizer so a chat
turn never loses its earlier-context notice.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import TypedDict

from axiom.infra.conversation_window import default_summarizer
from axiom.infra.structured_output import (
    SchemaValidationError,
    structured_output,
)

log = logging.getLogger(__name__)


class _SummarySchema(TypedDict):
    summary: str


_SYSTEM = (
    "You summarize dropped portions of chat history so the model keeps "
    "continuity without re-reading the full transcript. Produce a tight "
    "1-2 sentence summary naming the topics the user explored and what "
    "the assistant contributed. Never fabricate facts. Call the "
    "emit_summary tool once with the result."
)


def _format_transcript(messages: list[dict]) -> str:
    lines = []
    for m in messages:
        role = m.get("role", "?")
        content = str(m.get("content", "")).strip()
        if not content:
            continue
        # Truncate long turns so the summary prompt doesn't balloon.
        if len(content) > 500:
            content = content[:500] + "…"
        lines.append(f"[{role}] {content}")
    return "\n".join(lines)


def build_llm_summarizer(
    gateway,
    tool_name: str = "emit_summary",
    max_tokens: int = 256,
) -> Callable[[list[dict]], str]:
    """Factory for an LLM-backed summarizer matching the T0-2 protocol.

    Returned callable takes a list of dropped messages and returns a
    rendered summary string (already wrapped in the "[earlier context:
    ...]" convention so it visually matches the deterministic default).

    On any failure — gateway outage, schema validation error, zero
    messages — falls back to :func:`default_summarizer`. The conversation
    window always gets *some* summary.
    """

    def summarize(dropped: list[dict]) -> str:
        if not dropped:
            return ""
        try:
            prompt = (
                "Summarize these dropped chat turns:\n\n"
                + _format_transcript(dropped)
                + "\n\nCall emit_summary with a 1-2 sentence summary."
            )
            result = structured_output(
                gateway=gateway,
                schema=_SummarySchema,
                messages=[{"role": "user", "content": prompt}],
                system=_SYSTEM,
                tool_name=tool_name,
                max_tokens=max_tokens,
                task="summarization",
            )
            summary = str(result.value.get("summary", "")).strip()
            if not summary:
                return default_summarizer(dropped)
            return (
                f"[earlier context: {len(dropped)} messages omitted — "
                f"{summary}]"
            )
        except (SchemaValidationError, Exception) as exc:
            log.debug("LLM summarizer failed, falling back: %s", exc)
            return default_summarizer(dropped)

    return summarize
