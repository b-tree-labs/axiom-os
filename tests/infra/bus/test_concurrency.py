# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Concurrency safety per spec-event-bus.md §10.5.

The bus must safely handle:

1. Many publisher threads firing concurrently against a stable subscriber set.
2. A publisher thread + a register/unregister thread interleaving.
3. A handler that registers/unregisters another handler while inside
   dispatch (fire-time snapshot prevents the in-flight delivery from
   being affected, and the handler doesn't recurse).
"""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor

from axiom.infra.bus import EventBus


class TestEightThreadFireAndRegister:
    """8 publisher threads firing in a tight loop while a 9th
    alternately attaches/detaches a no-op subscriber. No exceptions,
    no missed events from the always-on subscriber, no double-firings."""

    def test_no_lost_events_under_contention(self):
        bus = EventBus()
        always_on_count = 0
        count_lock = threading.Lock()

        def always_on(subject, payload):
            nonlocal always_on_count
            with count_lock:
                always_on_count += 1

        bus.subscribe("test.>", always_on)

        n_publishers = 8
        per_thread = 200
        barrier = threading.Barrier(n_publishers + 1)
        stop = threading.Event()
        churn_subs: list = []

        def publisher() -> None:
            barrier.wait()
            for _ in range(per_thread):
                bus.publish("test.subject", {"k": "v"})

        def churner() -> None:
            barrier.wait()
            i = 0
            while not stop.is_set():
                if i % 2 == 0:
                    sub = bus.subscribe("test.>", lambda s, p: None)
                    churn_subs.append(sub)
                else:
                    if churn_subs:
                        bus.unsubscribe(churn_subs.pop(0))
                i += 1

        with ThreadPoolExecutor(max_workers=n_publishers + 1) as ex:
            futs = [ex.submit(publisher) for _ in range(n_publishers)]
            churn_fut = ex.submit(churner)

            for f in futs:
                f.result(timeout=10)
            stop.set()
            churn_fut.result(timeout=5)

        # The always-on handler must have received exactly every published
        # event — no losses, no duplicates from the snapshotting strategy.
        expected = n_publishers * per_thread
        assert always_on_count == expected, (
            f"expected {expected} deliveries, got {always_on_count}"
        )


class TestFireTimeSnapshotIsolation:
    """A handler that subscribes/unsubscribes during dispatch must NOT
    affect the in-flight delivery. The published event was snapshotted
    before any handler ran."""

    def test_subscribe_inside_handler_does_not_recurse(self):
        bus = EventBus()
        call_count = 0

        def handler(subject, payload):
            nonlocal call_count
            call_count += 1
            # Add another subscriber — must NOT receive THIS event.
            bus.subscribe("recursion", lambda s, p: None, source="dynamic")

        bus.subscribe("recursion", handler)
        bus.publish("recursion", {})

        # Original handler ran exactly once. The dynamically-added
        # subscriber didn't see this event (snapshot semantics).
        assert call_count == 1

    def test_unsubscribe_inside_handler_does_not_skip_others(self):
        bus = EventBus()
        ran: list[str] = []

        def first(subject, payload):
            ran.append("first")
            # Unsubscribe self mid-dispatch.
            bus.unsubscribe(first_sub)

        def second(subject, payload):
            ran.append("second")

        first_sub = bus.subscribe("x", first)
        bus.subscribe("x", second)

        bus.publish("x", {})

        # Both handlers ran; first's self-unsubscribe didn't disturb
        # the in-flight snapshot for `second`.
        assert "first" in ran
        assert "second" in ran


class TestRegistryMutex:
    """Sanity check on the registry mutex — concurrent attach/detach
    must not race. We hammer attach/detach pairs from many threads
    and verify the registry is empty at the end."""

    def test_paired_attach_detach_settles_clean(self):
        bus = EventBus()
        n_threads = 16
        rounds = 100
        barrier = threading.Barrier(n_threads)

        def churn() -> None:
            barrier.wait()
            for _ in range(rounds):
                sub = bus.subscribe("y.>", lambda s, p: None)
                bus.unsubscribe(sub)

        with ThreadPoolExecutor(max_workers=n_threads) as ex:
            futs = [ex.submit(churn) for _ in range(n_threads)]
            for f in futs:
                f.result(timeout=10)

        # No subscribers left.
        assert bus.subscribers_for("y.anything") == []
