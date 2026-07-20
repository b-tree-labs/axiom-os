# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Retry with backoff — centralized retry logic for network/service calls.

Usage:
    from axiom.infra.retry import retry

    @retry(max_attempts=3, backoff=2.0)
    def call_api():
        resp = requests.get(url, timeout=10)
        resp.raise_for_status()
        return resp.json()

    # Or inline:
    result = retry(max_attempts=3)(lambda: fragile_operation())
"""

from __future__ import annotations

import functools
import logging
import time
from typing import Any, TypeVar

logger = logging.getLogger(__name__)

T = TypeVar("T")


def retry(
    max_attempts: int = 3,
    backoff: float = 1.0,
    backoff_multiplier: float = 2.0,
    exceptions: tuple[type[BaseException], ...] = (Exception,),
    on_retry: Any = None,
):
    """Decorator/wrapper that retries on failure with exponential backoff.

    Args:
        max_attempts: Total attempts (including first try).
        backoff: Initial sleep between retries (seconds).
        backoff_multiplier: Multiply sleep by this after each retry.
        exceptions: Exception types to catch and retry on.
        on_retry: Optional callback(attempt, exception, sleep_time).
    """
    def decorator(fn):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            sleep = backoff
            last_exc: BaseException | None = None
            for attempt in range(1, max_attempts + 1):
                try:
                    return fn(*args, **kwargs)
                except exceptions as exc:
                    last_exc = exc
                    if attempt == max_attempts:
                        break
                    if on_retry:
                        on_retry(attempt, exc, sleep)
                    logger.debug(
                        "Retry %d/%d for %s after %.1fs: %s",
                        attempt, max_attempts, fn.__name__, sleep, exc,
                    )
                    time.sleep(sleep)
                    sleep *= backoff_multiplier
            raise last_exc  # type: ignore[misc]
        return wrapper
    return decorator


__all__ = ["retry"]
