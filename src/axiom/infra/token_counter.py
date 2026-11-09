# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Token counting for conversation-window management.

Tries ``tiktoken`` first (GPT-4 encoding is a close-enough proxy for
Anthropic as a general heuristic — good enough for a sliding-window
budget decision, and better than the legacy chars/4 estimate).

Falls back to a calibrated word-based heuristic when tiktoken is not
installed:
    tokens ≈ words / 0.75

Per-message overhead (for ``count_message``) accounts for role, the
message envelope, and JSON formatting around tool_calls.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable

_WORD_RE = re.compile(r"\w+|[^\w\s]", re.UNICODE)
_FALLBACK_WORDS_PER_TOKEN = 0.75
_MESSAGE_OVERHEAD_TOKENS = 4  # rough per-message envelope cost


def fallback_count(text: str) -> int:
    """Word-based heuristic with chars/4 floor for long non-space tokens."""
    if not text:
        return 0
    words = _WORD_RE.findall(text)
    if not words:
        return 0
    word_estimate = max(1, int(len(words) / _FALLBACK_WORDS_PER_TOKEN))
    # Guard against pathologically long single tokens (e.g. "x" * 5000);
    # chars/4 is a safe lower bound that tiktoken would produce.
    char_estimate = max(1, len(text) // 4)
    return max(word_estimate, char_estimate)


def _try_tiktoken() -> Callable[[str], int] | None:
    try:
        import tiktoken

        enc = tiktoken.get_encoding("cl100k_base")
        return lambda s: len(enc.encode(s)) if s else 0
    except Exception:
        return None


_TIKTOKEN_BACKEND: Callable[[str], int] | None = _try_tiktoken()


def count_tokens(text: str) -> int:
    """Count tokens in ``text``. Uses tiktoken if available."""
    if not text:
        return 0
    if _TIKTOKEN_BACKEND is not None:
        try:
            return _TIKTOKEN_BACKEND(text)
        except Exception:
            pass
    return fallback_count(text)


def count_message(msg: dict) -> int:
    """Token cost for one message (role + content + tool_calls + overhead)."""
    total = _MESSAGE_OVERHEAD_TOKENS
    content = msg.get("content") or ""
    total += count_tokens(content)
    tool_calls = msg.get("tool_calls") or []
    if tool_calls:
        total += count_tokens(json.dumps(tool_calls))
    tool_call_id = msg.get("tool_call_id")
    if tool_call_id:
        total += count_tokens(str(tool_call_id))
    return total


class TokenCounter:
    """Injectable token counter. Default uses :func:`count_tokens`.

    Unit tests pass a deterministic backend; production wiring can
    substitute a provider-specific counter (Anthropic's token_count API
    for Claude, tiktoken for OpenAI, etc.).
    """

    def __init__(self, backend: Callable[[str], int] | None = None) -> None:
        self._backend = backend or count_tokens

    def count(self, text: str) -> int:
        return self._backend(text)

    def count_message(self, msg: dict) -> int:
        """Count one message; uses backend for content + JSON overhead."""
        total = _MESSAGE_OVERHEAD_TOKENS
        total += self._backend(msg.get("content") or "")
        tool_calls = msg.get("tool_calls") or []
        if tool_calls:
            total += self._backend(json.dumps(tool_calls))
        return total
