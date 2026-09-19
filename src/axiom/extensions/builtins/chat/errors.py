# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Friendly error formatting for chat CLI.

Maps common exceptions to actionable user-visible messages.
Redacts secrets from fallback output.
"""

from __future__ import annotations

import re

# Patterns to redact from error messages before displaying
_REDACT_PATTERNS = [
    re.compile(r"sk-[A-Za-z0-9_-]+"),
    re.compile(r"Bearer\s+\S+"),
    re.compile(r"org-[A-Za-z0-9_-]+"),
    re.compile(r"[A-Z_]*KEY[A-Z_]*=\S+"),
]


def _redact(text: str) -> str:
    """Substitute known secret patterns with [redacted]."""
    for pattern in _REDACT_PATTERNS:
        text = pattern.sub("[redacted]", text)
    return text


def friendly(
    exc: Exception,
    *,
    providers: list[str] | None = None,
    provider: str | None = None,
) -> str:
    """Return a user-friendly error message for *exc*.

    Recognises common failure modes (missing provider, rate-limit, auth,
    network) and surfaces the most helpful next step. Falls back to the
    first line of the exception message with secrets redacted.
    """
    msg = str(exc).lower()
    raw = str(exc)

    # Provider not found
    if "provider not found" in msg or "no provider" in msg:
        avail = ""
        if providers:
            avail = f"  Available: {', '.join(providers)}\n"
        return f"\n  Provider not found.\n{avail}  Switch with: /model\n"

    # Session not found
    if "session" in msg and ("not found" in msg or "missing" in msg):
        return "\n  Session not found.\n  List sessions with: /sessions\n"

    # Rate limit / 429
    if "429" in raw or "rate limit" in msg or "too many requests" in msg:
        return (
            "\n  Rate limit reached. Wait a moment or switch providers with /model.\n"
        )

    # Network / connection errors
    if isinstance(exc, (ConnectionError, OSError)) or any(
        kw in msg
        for kw in ("connection", "network", "unreachable", "timed out", "timeout", "establish")
    ):
        target = provider or "the provider"
        return f"\n  Couldn't reach {target}. Check your network or run /model to switch.\n"

    # Auth / 401
    if "401" in raw or "unauthorized" in msg or "invalid api key" in msg or "api key" in msg:
        return "\n  API key rejected. Check your key or run /model to switch providers.\n"

    # Fallback: first line of exception, with secrets redacted
    first_line = raw.splitlines()[0] if raw.splitlines() else raw
    return f"\n  {_redact(first_line)}\n"
