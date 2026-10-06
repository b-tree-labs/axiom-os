# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Post-filter policy-breach detection (#40) — deterministic gate on LLM output.

Per Rezazadeh et al. 2025 §6 admission: LLMs occasionally leak under
policy enforcement at retrieval time (the model returns content that
references fragments the current user can't see, even when those
fragments weren't in the retrieved set).

This module is the deterministic post-check: after the LLM generates
a response, re-verify against the visible-fragment set. If the
response cites a fragment id the user can't see, or quotes ≥N words
verbatim from a non-visible fragment's content, flag or redact.

Detection is signal-limited on purpose:
- Direct fragment-id citation (UUID pattern match).
- Verbatim content-quote (word-run overlap).

Semantic paraphrase detection is intentionally NOT here — it
requires another LLM and is probabilistic. This module is the
deterministic belt-and-suspenders gate per the
deterministic-vs-model-mediated boundary memory.

Consumes memory/access.py's visibility set; sits on the read path
AFTER the LLM generation step.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass, field

from .fragment import MemoryFragment

# ---------------------------------------------------------------------------
# Result
# ---------------------------------------------------------------------------


@dataclass
class BreachCheckResult:
    output: str
    is_clean: bool
    breaches: list[dict] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


_UUID_RE = re.compile(
    r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{8,12}\b",
    re.IGNORECASE,
)


def _collect_text(content) -> str:
    """Return all string content in a fragment as one string (for quote matching)."""
    parts: list[str] = []

    def walk(obj):
        if isinstance(obj, str):
            parts.append(obj)
        elif isinstance(obj, dict):
            for v in obj.values():
                walk(v)
        elif isinstance(obj, list):
            for v in obj:
                walk(v)

    walk(content)
    return "\n".join(parts)


def _contains_quote(haystack: str, needle: str, min_words: int) -> bool:
    """Does haystack contain a contiguous N-word run from needle?

    Word tokens are whitespace-split, lowercased for comparison.
    Cheap sliding-window search.
    """
    needle_tokens = needle.lower().split()
    haystack_lower = haystack.lower()
    if len(needle_tokens) < min_words:
        return False
    for i in range(len(needle_tokens) - min_words + 1):
        run = " ".join(needle_tokens[i : i + min_words])
        if run in haystack_lower:
            return True
    return False


# ---------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------


def check_llm_output(
    output: str,
    visible_fragments: Iterable[MemoryFragment],
    all_fragments: Iterable[MemoryFragment],
    min_quote_words: int = 10,
) -> BreachCheckResult:
    """Return a BreachCheckResult flagging id citations + quote leaks."""
    visible_ids = {f.id for f in visible_fragments}
    all_list = list(all_fragments)

    breaches: list[dict] = []

    # 1) Direct UUID citation of a non-visible fragment
    for match in _UUID_RE.finditer(output):
        cited = match.group(0)
        for f in all_list:
            if f.id == cited and f.id not in visible_ids:
                breaches.append({
                    "fragment_id": f.id,
                    "reason": "id_citation",
                    "match": cited,
                })

    # 2) Verbatim quote from a non-visible fragment's content
    for f in all_list:
        if f.id in visible_ids:
            continue
        text = _collect_text(f.content)
        if text and _contains_quote(output, text, min_quote_words):
            breaches.append({
                "fragment_id": f.id,
                "reason": "content_quote",
            })

    return BreachCheckResult(
        output=output,
        is_clean=len(breaches) == 0,
        breaches=breaches,
    )


# ---------------------------------------------------------------------------
# Redaction
# ---------------------------------------------------------------------------


def redact_breaches(
    output: str,
    visible_fragments: Iterable[MemoryFragment],
    all_fragments: Iterable[MemoryFragment],
    min_quote_words: int = 10,
    placeholder: str = "[REDACTED]",
) -> str:
    """Return the output with any detected breaches redacted."""
    visible_ids = {f.id for f in visible_fragments}
    all_list = list(all_fragments)

    redacted = output

    # Redact non-visible UUID citations
    for f in all_list:
        if f.id not in visible_ids:
            redacted = redacted.replace(f.id, placeholder)

    # Redact verbatim content quotes
    for f in all_list:
        if f.id in visible_ids:
            continue
        text = _collect_text(f.content)
        if not text:
            continue
        tokens = text.split()
        if len(tokens) < min_quote_words:
            continue
        # Try progressively longer windows; replace first match
        for window_size in range(len(tokens), min_quote_words - 1, -1):
            for i in range(len(tokens) - window_size + 1):
                run = " ".join(tokens[i : i + window_size])
                if run.lower() in redacted.lower():
                    # Case-insensitive replace (preserve case elsewhere)
                    pattern = re.compile(re.escape(run), re.IGNORECASE)
                    redacted = pattern.sub(placeholder, redacted, count=1)
                    break
            else:
                continue
            break

    return redacted
