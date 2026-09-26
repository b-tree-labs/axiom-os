# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Context-budget helpers — fit a request into a model's usable window.

A served model exposes a finite context window, and with parallelism the
*per-request* window is often far smaller than the nominal figure: a server
launched with ``--parallel N`` divides its total context into N per-request
slots. When a request exceeds its slot the backend rejects it outright, and
telling the user to "retry" is wrong — the same over-limit request fails
identically. This module gives a serving loop the two pieces it needs to
degrade gracefully instead of dead-ending:

1. :func:`is_context_overflow` — recognize the backend's over-limit error so
   the loop can react to it specifically (not as a generic 5xx).
2. :func:`trim_messages_to_budget` / :func:`cap_text_to_tokens` — shrink the
   request (drop oldest turns; cap injected retrieval) so a retry can succeed.

Estimation is deliberately cheap and model-agnostic (character heuristic); it
is used to *decide what to drop*, not to promise an exact token count. The
serving loop should treat the budget as a soft target and still catch a real
overflow error as the backstop. Domain-agnostic: names no consumer.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any

# Substrings various OpenAI-compatible backends emit when a prompt overflows
# the (per-slot) context window. Kept broad; matched case-insensitively.
_OVERFLOW_RE = re.compile(
    r"exceeds the available context size"
    r"|maximum context length"
    r"|context (?:window|length) (?:exceeded|is too|too long)"
    r"|too many tokens"
    r"|reduce the length of the (?:messages|prompt)",
    re.IGNORECASE,
)


def is_context_overflow(error: str | None) -> bool:
    """True if ``error`` looks like a backend context-overflow rejection."""
    return bool(error) and bool(_OVERFLOW_RE.search(error))


def estimate_tokens(text: str | None) -> int:
    """Cheap, slightly conservative token estimate (~4 chars/token)."""
    if not text:
        return 0
    return max(1, len(text) // 4)


def _message_text(message: Mapping[str, Any]) -> str:
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [p.get("text", "") for p in content if isinstance(p, Mapping)]
        return " ".join(parts)
    return "" if content is None else str(content)


def estimate_messages_tokens(
    messages: Sequence[Mapping[str, Any]], per_message_overhead: int = 4
) -> int:
    """Sum the per-message estimates plus a small per-message framing overhead."""
    return sum(estimate_tokens(_message_text(m)) + per_message_overhead for m in messages)


def cap_text_to_tokens(text: str, max_tokens: int) -> str:
    """Truncate ``text`` to roughly ``max_tokens`` (by the same 4-char heuristic),
    appending an ellipsis marker so the model knows it was cut."""
    if max_tokens <= 0:
        return ""
    limit_chars = max_tokens * 4
    if len(text) <= limit_chars:
        return text
    return text[:limit_chars].rstrip() + " …[truncated]"


def trim_messages_to_budget(
    messages: Sequence[Mapping[str, Any]],
    budget_tokens: int,
    *,
    keep_last: int = 2,
    preserve_leading_system: bool = True,
) -> tuple[list[Mapping[str, Any]], int]:
    """Drop the OLDEST droppable turns until the estimate fits ``budget_tokens``.

    Always keeps the final ``keep_last`` messages (the live exchange) and any
    leading system message(s). Returns ``(trimmed, dropped_count)``. If the
    floor (system + last ``keep_last``) still exceeds the budget, returns that
    floor — the caller must then shrink content another way (e.g. cap injected
    retrieval with :func:`cap_text_to_tokens`) or accept the overflow backstop.
    """
    msgs = list(messages)
    head = 0
    if preserve_leading_system:
        while head < len(msgs) and msgs[head].get("role") == "system":
            head += 1

    dropped = 0
    while estimate_messages_tokens(msgs) > budget_tokens:
        drop_hi = len(msgs) - keep_last
        if drop_hi <= head:
            break  # only protected head + tail remain
        del msgs[head]
        dropped += 1
    return msgs, dropped


__all__ = [
    "is_context_overflow",
    "estimate_tokens",
    "estimate_messages_tokens",
    "cap_text_to_tokens",
    "trim_messages_to_budget",
]
