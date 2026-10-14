# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Adapter: drive a ResearchLoop from OpenAI chat messages."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from axiom.research import ResearchLoop, ResearchQuestion


def research_loop_backend(loop: ResearchLoop) -> Callable[..., str]:
    """Return a ChatCompletionsHandler backend that runs the research loop.

    The last user message is treated as the research question. The loop's
    final answer is returned as the assistant content.
    """

    def backend(messages: list[dict[str, Any]], *, model: str, **_: Any) -> str:
        question_text = _last_user_text(messages)
        if question_text is None:
            raise ValueError("no user message in request")
        result = loop.run(ResearchQuestion(text=question_text, metadata={"model": model}))
        return result.final_answer

    return backend


def _last_user_text(messages: list[dict[str, Any]]) -> str | None:
    for m in reversed(messages):
        if m.get("role") == "user":
            return str(m.get("content", ""))
    return None
