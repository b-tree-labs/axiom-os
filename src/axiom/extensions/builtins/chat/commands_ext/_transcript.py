# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""What /copy and /export share: replies, the secret scan, and the redaction (ADR-168, C6/C7).

One scanner serves both commands, so a reply that ``/copy`` refuses is a reply ``/export`` refuses. It is
built on the classifier the memory serving boundary already uses (``axiom.memory.serving``), widened with the
key shapes a chat is likely to hold that the memory classifier deliberately leaves out (it is tuned for high
precision on stored memories; a transcript leaving the machine can afford more caution).
"""

from __future__ import annotations

import re
from collections import Counter
from typing import Any

from axiom.memory.serving import _SECRET_PATTERNS as _MEMORY_PATTERNS

# Most specific first: where two classes match the same text, the earlier class names it.
_EXTRA: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("stripe_key", re.compile(r"\b[sr]k_(?:live|test)_[A-Za-z0-9]{16,}\b")),
    ("google_api_key", re.compile(r"\bAIza[0-9A-Za-z_-]{30,}\b")),
    ("anthropic_key", re.compile(r"\bsk-ant-[A-Za-z0-9_-]{16,}")),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}")),
    ("bearer_token", re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]{16,}")),
    ("env_key_assignment", re.compile(r"\b[A-Z][A-Z0-9_]*(?:KEY|SECRET|TOKEN|PASSWORD)=[^\s'\"]{8,}")),
)

#: (class, pattern) in priority order. The memory classifier's catch-all ``labeled_secret`` goes last.
PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = tuple(
    [p for p in _MEMORY_PATTERNS if p[0] != "labeled_secret"]
    + list(_EXTRA)
    + [p for p in _MEMORY_PATTERNS if p[0] == "labeled_secret"]
)

_REDACTED = re.compile(r"\[redacted:[a-z_]+\]")


def _spans(text: str) -> list[tuple[int, int, str]]:
    """Non-overlapping (start, end, class) matches; the highest-priority class wins an overlap."""
    found: list[tuple[int, int, int, str]] = []
    for prio, (name, pat) in enumerate(PATTERNS):
        for m in pat.finditer(text):
            found.append((prio, m.start(), m.end(), name))
    found.sort()  # by priority, then position: the most specific class claims its text first
    claimed: list[tuple[int, int, str]] = []
    for _prio, start, end, name in found:
        if any(start < e and s < end for s, e, _ in claimed):
            continue
        claimed.append((start, end, name))
    return sorted(claimed)


def redact_text(text: str) -> tuple[str, Counter[str]]:
    counts: Counter[str] = Counter()
    if not text:
        return text, counts
    pieces: list[str] = []
    pos = 0
    for start, end, name in _spans(text):
        pieces.append(text[pos:start])
        pieces.append(f"[redacted:{name}]")
        counts[name] += 1
        pos = end
    pieces.append(text[pos:])
    return "".join(pieces), counts


def redact(obj: Any) -> tuple[Any, Counter[str]]:
    """Redact every string in a JSON-shaped structure (keys are left alone). Returns a new structure."""
    counts: Counter[str] = Counter()

    def walk(v: Any) -> Any:
        if isinstance(v, str):
            new, c = redact_text(v)
            counts.update(c)
            return new
        if isinstance(v, dict):
            return {k: walk(x) for k, x in v.items()}
        if isinstance(v, (list, tuple)):
            return [walk(x) for x in v]
        return v

    return walk(obj), counts


def scan(obj: Any) -> Counter[str]:
    """Secret classes found anywhere in ``obj`` and how many of each. Never returns the matched text."""
    return redact(obj)[1]


def describe(counts: Counter[str]) -> str:
    return ", ".join(f"{name} x{n}" for name, n in sorted(counts.items()))


def replies(agent: Any) -> list[Any]:
    """The assistant messages in the session, oldest first."""
    session = getattr(agent, "session", None)
    return [m for m in getattr(session, "messages", []) if m.role == "assistant" and m.content.strip()]


def brand_cli() -> str:
    try:
        from axiom.infra.branding import get_branding

        return get_branding().cli_name
    except Exception:  # noqa: BLE001
        return "axi"


def describe_dict(counts: dict[str, int]) -> str:
    return describe(Counter(counts))
