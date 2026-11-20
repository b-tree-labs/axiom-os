# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""The frame bus: one source, held once, fanned out to every subscriber (spec-video §Frame bus).

Per source and in process. A subscription is a bounded queue; when it is full the
OLDEST frame is dropped and counted, so a slow subscriber sees fresh frames late
rather than stale frames forever. Publishing never blocks: one slow subscriber
never delays another, or the decoder. Counters per subscription: ``delivered``,
``dropped``, ``last_frame_age_s``.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from collections.abc import Callable
from typing import Any

from .frame import Frame

#: Frames a subscription holds before it starts dropping its oldest.
DEFAULT_MAXSIZE = 8


class Subscription:
    """One consumer's view of the bus: a bounded, drop-oldest queue of frames."""

    name: str
    maxsize: int

    def __init__(self, name: str, maxsize: int, *, clock: Callable[[], float]) -> None:
        if maxsize < 1:
            raise ValueError(
                f"subscription {name!r}: maxsize must hold at least one frame, got {maxsize}"
            )
        self.name = name
        self.maxsize = maxsize
        self._clock = clock
        self._queue: deque[Frame] = deque()
        self._ready = threading.Condition()
        self._delivered = 0
        self._dropped = 0
        self._last_delivered_at: float | None = None
        self._closed = False

    def get(self, timeout: float | None = None) -> Frame | None:
        """The oldest queued frame, waiting up to ``timeout``; ``None`` on timeout or once closed."""
        with self._ready:
            if not self._ready.wait_for(lambda: self._queue or self._closed, timeout=timeout):
                return None
            return self._take()

    def get_nowait(self) -> Frame | None:
        with self._ready:
            return self._take()

    def _take(self) -> Frame | None:
        if not self._queue:
            return None
        self._delivered += 1
        self._last_delivered_at = self._clock()
        return self._queue.popleft()

    @property
    def delivered(self) -> int:
        return self._delivered

    @property
    def dropped(self) -> int:
        return self._dropped

    @property
    def queued(self) -> int:
        return len(self._queue)

    @property
    def last_frame_age_s(self) -> float | None:
        """Seconds since this subscription last handed a frame over; ``None`` if it never has."""
        at = self._last_delivered_at
        return None if at is None else max(0.0, self._clock() - at)

    @property
    def closed(self) -> bool:
        return self._closed

    def counters(self) -> dict[str, Any]:
        """``name``, ``delivered``, ``dropped``, ``queued``, ``last_frame_age_s``."""
        with self._ready:
            return {
                "name": self.name,
                "delivered": self._delivered,
                "dropped": self._dropped,
                "queued": len(self._queue),
                "last_frame_age_s": self.last_frame_age_s,
            }

    def offer(self, frame: Frame) -> bool:
        """Queue ``frame``, dropping the oldest if full. Never blocks. False once closed."""
        with self._ready:
            if self._closed:
                return False
            if len(self._queue) >= self.maxsize:
                self._queue.popleft()
                self._dropped += 1
            self._queue.append(frame)
            self._ready.notify()
            return True

    def close(self) -> None:
        """Stop receiving and wake anyone waiting in :meth:`get`."""
        with self._ready:
            self._closed = True
            self._queue.clear()
            self._ready.notify_all()


class FrameBus:
    """The bus for one stream."""

    stream: str

    def __init__(self, stream: str, *, clock: Callable[[], float] = time.monotonic) -> None:
        self.stream = stream
        self._clock = clock
        self._lock = threading.Lock()
        self._subs: dict[str, Subscription] = {}
        self._published = 0
        self._dropped_by_gone = 0

    def subscribe(self, name: str, *, maxsize: int = DEFAULT_MAXSIZE) -> Subscription:
        """A new subscription. Names are unique per bus; ``maxsize`` is at least 1."""
        sub = Subscription(name, maxsize, clock=self._clock)
        with self._lock:
            if name in self._subs:
                raise ValueError(
                    f"stream {self.stream!r} already has a subscription named {name!r}"
                )
            self._subs[name] = sub
        return sub

    def unsubscribe(self, name: str) -> None:
        with self._lock:
            sub = self._subs.pop(name, None)
            if sub is not None:
                self._dropped_by_gone += sub.dropped
        if sub is not None:
            sub.close()

    def publish(self, frame: Frame) -> int:
        """Offer ``frame`` to every subscription without waiting. Returns how many were offered it."""
        with self._lock:
            self._published += 1
            subs = list(self._subs.values())
        return sum(1 for sub in subs if sub.offer(frame))

    def subscriptions(self) -> list[Subscription]:
        with self._lock:
            return list(self._subs.values())

    @property
    def published(self) -> int:
        return self._published

    @property
    def dropped(self) -> int:
        """Frames dropped across every subscription, including ones since unsubscribed."""
        with self._lock:
            return self._dropped_by_gone + sum(s.dropped for s in self._subs.values())

    def counters(self) -> dict[str, Any]:
        """``stream``, ``published``, ``dropped`` and each subscription's counters."""
        return {
            "stream": self.stream,
            "published": self.published,
            "dropped": self.dropped,
            "subscriptions": [s.counters() for s in self.subscriptions()],
        }

    def close(self) -> None:
        with self._lock:
            subs = list(self._subs.values())
            self._subs.clear()
            self._dropped_by_gone += sum(s.dropped for s in subs)
        for sub in subs:
            sub.close()


__all__ = ["DEFAULT_MAXSIZE", "FrameBus", "Subscription"]
