# Copyright (c) 2026 B-Tree Ventures, LLC
# SPDX-License-Identifier: Apache-2.0

"""Graceful tool-failure handling.

Three layers:

1. ``classify_exception(exc)`` — maps a Python exception to a typed
   ``ToolError`` with a small, stable taxonomy: transient, permission_denied,
   not_found, invalid_input, unknown.

2. ``run_with_retry(fn, max_attempts=...)`` — runs a tool function with
   automatic retry on transient failures using full-jitter exponential
   backoff. Retryable errors (transient + unknown) get retried up to
   ``max_attempts`` times; non-retryable errors (permission_denied,
   not_found, invalid_input) bubble immediately. Final exception is
   raised if every attempt fails.

3. ``safe_run(fn, tool_name=..., max_attempts=...)`` — top-level entry
   point used by the chat agent's tool-execution path. Returns either
   the function's success result OR a typed error dict; never raises.
   The error dict is what the LLM sees on tool failure.

Federation-aware fallback (``federation_fallback_eligible`` field on
ToolError) is data-model-only in v0; the live wiring lands when
ComputeDecomposition / ADR-040 ships peer tool dispatch.
"""

from __future__ import annotations

import logging
import random
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Literal

log = logging.getLogger(__name__)

ErrorType = Literal[
    "transient",
    "permission_denied",
    "not_found",
    "invalid_input",
    "unknown",
]

_DEFAULT_MAX_ATTEMPTS = 3
_BACKOFF_BASE_SECONDS = 0.5

# Errors that are *worth* retrying.
_RETRYABLE_ERROR_TYPES: frozenset[str] = frozenset({"transient", "unknown"})

# Errors where federation fallback (try the same logical tool on a peer)
# might help. Auth + invalid-input are local problems and won't be fixed
# by re-running on a peer with the same credentials/schema.
_FEDERATION_FALLBACK_ELIGIBLE: frozenset[str] = frozenset({"transient", "not_found", "unknown"})


@dataclass(frozen=True)
class ToolError:
    error_type: ErrorType
    error_class: str
    message: str
    retryable: bool
    federation_fallback_eligible: bool
    attempts: int = 1
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "error": self.message,
            "error_type": self.error_type,
            "error_class": self.error_class,
            "message": self.message,
            "retryable": self.retryable,
            "federation_fallback_eligible": self.federation_fallback_eligible,
            "attempts": self.attempts,
        }
        if self.extra:
            d["extra"] = self.extra
        return d


def classify_exception(exc: BaseException) -> ToolError:
    """Map a Python exception to a typed ``ToolError``."""
    error_class = type(exc).__name__
    msg = str(exc) or error_class

    # Network / IO transient — duck-typed via requests when available + by
    # standard-library exceptions to keep this module dependency-light.
    transient_classes: tuple[type, ...] = (TimeoutError,)
    try:
        import requests as _requests  # type: ignore[import-untyped]

        transient_classes = transient_classes + (
            _requests.exceptions.ConnectionError,
            _requests.exceptions.Timeout,
            _requests.exceptions.ChunkedEncodingError,
        )
    except ImportError:
        pass

    if isinstance(exc, transient_classes):
        return ToolError(
            error_type="transient",
            error_class=error_class,
            message=msg,
            retryable=True,
            federation_fallback_eligible=True,
        )

    if isinstance(exc, PermissionError):
        return ToolError(
            error_type="permission_denied",
            error_class=error_class,
            message=msg,
            retryable=False,
            federation_fallback_eligible=False,
        )

    if isinstance(exc, FileNotFoundError):
        return ToolError(
            error_type="not_found",
            error_class=error_class,
            message=msg,
            retryable=False,
            federation_fallback_eligible=True,
        )

    if isinstance(exc, (ValueError, KeyError, TypeError)):
        return ToolError(
            error_type="invalid_input",
            error_class=error_class,
            message=msg,
            retryable=False,
            federation_fallback_eligible=False,
        )

    # Unknown — give the benefit of the doubt for a single retry. Could be
    # a transient network blip wrapped in a vendor exception we don't
    # recognize. The retry is cheap; the false-negative cost is high.
    return ToolError(
        error_type="unknown",
        error_class=error_class,
        message=msg,
        retryable=True,
        federation_fallback_eligible=True,
    )


def _backoff_seconds(attempt: int) -> float:
    """Full-jitter exponential backoff. Same pattern as gateway resilience.

    Sleep = random(0, base * 2**attempt) — AWS-blessed; prevents
    thundering-herd resync after coordinated upstream failure.
    """
    return random.uniform(0, _BACKOFF_BASE_SECONDS * (2 ** max(0, attempt - 1)))


def run_with_retry(
    fn: Callable[[], Any],
    *,
    max_attempts: int = _DEFAULT_MAX_ATTEMPTS,
) -> Any:
    """Run ``fn``; retry transient/unknown failures with jittered backoff.

    Raises the *last* exception if every attempt fails. Non-retryable
    errors (auth, invalid input, not-found) bubble after the first attempt.
    """
    if max_attempts < 1:
        raise ValueError("max_attempts must be >= 1")
    last_exc: BaseException | None = None
    for attempt in range(1, max_attempts + 1):
        try:
            return fn()
        except BaseException as exc:
            last_exc = exc
            err = classify_exception(exc)
            if not err.retryable:
                raise
            if attempt >= max_attempts:
                raise
            wait = _backoff_seconds(attempt)
            log.warning(
                "Tool transient failure (attempt %d/%d, %s), retrying in %.2fs: %s",
                attempt, max_attempts, err.error_type, wait, err.message,
            )
            time.sleep(wait)
    # Defensive — loop above always either returns or raises.
    assert last_exc is not None
    raise last_exc


def safe_run(
    fn: Callable[[], Any],
    *,
    tool_name: str,
    max_attempts: int = _DEFAULT_MAX_ATTEMPTS,
) -> dict[str, Any]:
    """Run a tool function and return either the success result OR a typed
    error dict. Never raises.

    The dict shape on failure carries the typed taxonomy + retry attempt
    count + federation-fallback eligibility, so the LLM can make an
    intelligent next move (try a different tool, ask the user, route
    to a peer once that path lands).
    """
    attempt_counter = {"n": 0}

    def _wrapped():
        attempt_counter["n"] += 1
        return fn()

    try:
        return run_with_retry(_wrapped, max_attempts=max_attempts)
    except BaseException as exc:
        err = classify_exception(exc)
        # Patch the attempts count from the actual run, not the default.
        err = ToolError(
            error_type=err.error_type,
            error_class=err.error_class,
            message=err.message,
            retryable=err.retryable,
            federation_fallback_eligible=err.federation_fallback_eligible,
            attempts=attempt_counter["n"],
            extra=err.extra,
        )
        d = err.to_dict()
        d["tool_name"] = tool_name
        return d


__all__ = [
    "ErrorType",
    "ToolError",
    "classify_exception",
    "run_with_retry",
    "safe_run",
]
