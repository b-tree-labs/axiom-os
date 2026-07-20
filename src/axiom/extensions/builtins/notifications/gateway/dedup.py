# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0

"""Inbound idempotency — per-(vendor, event-id) dedup (ADR-067 G2).

Slack retries webhooks with the same ``event_id``; Twilio retries with
the same ``MessageSid``. Without dedup an agent action fires twice. This
is the in-memory LRU+TTL used in tests and as the SEC-1 default; the
Postgres-backed store swaps in behind the same interface in PR-9.
"""

from __future__ import annotations

import time
from collections import OrderedDict
from collections.abc import Callable

_DEFAULT_TTL = 24 * 60 * 60  # 24h, matching Slack's retry window
_DEFAULT_MAXSIZE = 8192


class DedupCache:
    def __init__(
        self,
        *,
        maxsize: int = _DEFAULT_MAXSIZE,
        ttl_seconds: float = _DEFAULT_TTL,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._maxsize = maxsize
        self._ttl = ttl_seconds
        self._clock = clock
        self._seen: OrderedDict[tuple[str, str], float] = OrderedDict()

    def seen_or_add(self, vendor: str, event_id: str) -> bool:
        """Return True if ``(vendor, event_id)`` was already seen (a dup).

        First sight records it and returns False. A blank ``event_id`` is
        never deduped (treated as always-new) so undecodable events still
        flow rather than collapsing onto one empty key.
        """
        if not event_id:
            return False
        now = self._clock()
        self._evict(now)
        key = (vendor, event_id)
        if key in self._seen:
            self._seen.move_to_end(key)
            return True
        self._seen[key] = now + self._ttl
        if len(self._seen) > self._maxsize:
            self._seen.popitem(last=False)
        return False

    def _evict(self, now: float) -> None:
        expired = [k for k, exp in self._seen.items() if exp <= now]
        for k in expired:
            del self._seen[k]


__all__ = ["DedupCache"]
