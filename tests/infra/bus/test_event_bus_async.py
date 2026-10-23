# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tests for async dispatch.

The bus owns a private daemon-thread asyncio loop, started lazily on first
async subscribe. Authors who already own a chat/HTTP loop pass it in via
`async_loop=`. `publish()` does NOT await async handlers — it schedules
them and returns immediately.
"""

from __future__ import annotations

import asyncio
import threading
import time
from collections import deque

from axiom.infra.bus import EventBus


class TestAsyncDispatch:
    def test_async_handler_receives_event(self):
        bus = EventBus()
        received: deque = deque()
        done = threading.Event()

        async def handler(subject, payload):
            received.append((subject, payload))
            done.set()

        bus.subscribe_async("test.async", handler)
        bus.publish("test.async", {"x": 1})

        # Async handler runs on a background loop; wait briefly.
        assert done.wait(timeout=2.0)
        assert list(received) == [("test.async", {"x": 1})]

    def test_publish_does_not_block_on_slow_async_handler(self):
        bus = EventBus()
        started = threading.Event()
        finish = threading.Event()
        finished = threading.Event()

        async def slow(subject, payload):
            started.set()
            # Wait for the test to release us.
            while not finish.is_set():
                await asyncio.sleep(0.01)
            finished.set()

        bus.subscribe_async("slow", slow)

        t0 = time.monotonic()
        bus.publish("slow", {})
        t1 = time.monotonic()

        # publish() should return promptly — well under the handler's lifetime.
        assert (t1 - t0) < 0.5, f"publish blocked for {t1 - t0:.2f}s"
        # Handler did start.
        assert started.wait(timeout=2.0)
        # Cleanup.
        finish.set()
        assert finished.wait(timeout=2.0)

    def test_private_loop_starts_lazily(self):
        bus = EventBus()
        # Before any async use, the bus has not started its loop.
        assert not bus._has_private_loop  # noqa: SLF001  intentional inspection.

        async def noop(s, p):
            pass

        bus.subscribe_async("x", noop)
        # Now the private loop is up.
        assert bus._has_private_loop  # noqa: SLF001

    def test_supplied_loop_is_honored(self):
        # If the caller passes its own loop, the bus uses it instead of
        # spawning a private one.
        loop = asyncio.new_event_loop()
        loop_thread = threading.Thread(target=loop.run_forever, daemon=True)
        loop_thread.start()

        try:
            bus = EventBus(async_loop=loop)
            received: deque = deque()
            done = threading.Event()

            async def handler(s, p):
                received.append(s)
                done.set()

            bus.subscribe_async("via.supplied.loop", handler)
            bus.publish("via.supplied.loop", {})

            assert done.wait(timeout=2.0)
            assert list(received) == ["via.supplied.loop"]
            # No private loop spun up — supplied loop did the work.
            assert not bus._has_private_loop  # noqa: SLF001
        finally:
            loop.call_soon_threadsafe(loop.stop)
            loop_thread.join(timeout=2.0)


class TestAsyncMixed:
    def test_sync_and_async_subscribers_both_fire(self):
        bus = EventBus()
        sync_received: list = []
        async_received: deque = deque()
        done = threading.Event()

        bus.subscribe("mix", lambda s, p: sync_received.append(s))

        async def ahandler(s, p):
            async_received.append(s)
            done.set()

        bus.subscribe_async("mix", ahandler)

        bus.publish("mix", {})

        # Sync ran inline.
        assert sync_received == ["mix"]
        # Async ran on the loop.
        assert done.wait(timeout=2.0)
        assert list(async_received) == ["mix"]


class TestAsyncSubscriberMetadata:
    def test_subscription_marked_async(self):
        bus = EventBus()

        async def h(s, p):
            pass

        sub = bus.subscribe_async("x.>", h, priority=50, fail_mode="ignore")
        assert sub.is_async is True
        assert sub.priority == 50
        assert sub.fail_mode == "ignore"
