# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""NATS-shape subject grammar and matcher.

Grammar (per spec-event-bus.md §5):

    subject       := token ('.' token)*
    pattern       := pattern-token ('.' pattern-token)* | pattern-prefix '.>'
    pattern-token := token | '*'
    token         := [a-z0-9_]+

Matching rules:

- `*` matches exactly one token between dots.
- `>` matches one or more tokens; only legal as the final element of a pattern.
- Subjects/patterns are case-sensitive ASCII (lowercase letters, digits, underscores).

Both `validate_pattern` and `subject_matches` reject malformed input with a
clear `InvalidSubjectError`.
"""

from __future__ import annotations

import re

_TOKEN_RE = re.compile(r"^[a-z0-9_]+$")


class InvalidSubjectError(ValueError):
    """Raised when a subject or pattern violates the NATS-shape grammar."""


def _validate_token(token: str, *, allow_wildcards: bool, is_tail: bool, raw: str) -> None:
    """Validate a single dot-separated token.

    Args:
        token: The token text to validate (between dots).
        allow_wildcards: Whether `*` and `>` are legal in this position.
        is_tail: Whether this is the final token in the pattern. `>` only
            allowed when `is_tail=True`.
        raw: The original full pattern, included in error messages.
    """
    if token == "":
        raise InvalidSubjectError(
            f"empty token in subject/pattern {raw!r}"
            " (consecutive dots, leading dot, or trailing dot)",
        )

    if allow_wildcards:
        if token == "*":
            return
        if token == ">":
            if not is_tail:
                raise InvalidSubjectError(
                    f"`>` is only valid as the final token in pattern {raw!r}",
                )
            return

    if not _TOKEN_RE.fullmatch(token):
        raise InvalidSubjectError(
            f"invalid token {token!r} in {raw!r}: tokens must match"
            " [a-z0-9_]+ (or be `*` / final `>` for patterns)",
        )


def validate_pattern(pattern: str) -> None:
    """Validate a subscription pattern. Raises InvalidSubjectError on failure."""
    if not pattern:
        raise InvalidSubjectError("empty pattern")

    tokens = pattern.split(".")
    for i, tok in enumerate(tokens):
        _validate_token(
            tok,
            allow_wildcards=True,
            is_tail=(i == len(tokens) - 1),
            raw=pattern,
        )


def validate_subject(subject: str) -> None:
    """Validate a concrete (publish-time) subject — no wildcards allowed.

    Raises InvalidSubjectError on failure.
    """
    if not subject:
        raise InvalidSubjectError("empty subject")

    tokens = subject.split(".")
    for tok in tokens:
        _validate_token(tok, allow_wildcards=False, is_tail=False, raw=subject)


def subject_matches(subject: str, pattern: str) -> bool:
    """Return True iff `subject` matches the NATS-shape `pattern`.

    Both arguments are validated. The first argument must be a concrete
    subject (no wildcards); the second may use `*` or `>`. Invalid input
    raises InvalidSubjectError.
    """
    validate_subject(subject)
    validate_pattern(pattern)

    pat_tokens = pattern.split(".")
    sub_tokens = subject.split(".")

    # Tail-`>` matches one or more remaining tokens.
    if pat_tokens[-1] == ">":
        prefix = pat_tokens[:-1]
        # Need at least one token to satisfy `>`.
        if len(sub_tokens) <= len(prefix):
            return False
        # Match the prefix tokens (`*` allowed).
        for pt, st in zip(prefix, sub_tokens[: len(prefix)], strict=True):
            if pt == "*":
                continue
            if pt != st:
                return False
        return True

    # Non-`>` pattern: must have exact token count.
    if len(pat_tokens) != len(sub_tokens):
        return False
    for pt, st in zip(pat_tokens, sub_tokens, strict=True):
        if pt == "*":
            continue
        if pt != st:
            return False
    return True
