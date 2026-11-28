# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""The frame bus fans one source out to many subscribers and never waits on any of them.

spec-video §Frame bus: per source, in process; ``subscribe(name, maxsize=…)``; a full
subscription drops its OLDEST frame and counts it; one slow subscriber never delays
another or the decoder; per subscription ``delivered``, ``dropped``,
``last_frame_age_s``. VID-004 (fan out through a bounded bus) and VID-009 (a
source is captured whether or not anything consumes it).
"""

from __future__ import annotations

import threading
import time

import pytest

from axiom.extensions.builtins.video.bus import FrameBus
from axiom.extensions.builtins.video.frame import frame_from_bytes

from .fakes import frame_bytes


def _frame(seq: int, *, session: str = "s1"):
    return frame_from_bytes(
        frame_bytes(4, 2, seq),
        width=4,
        height=2,
        stream="cam",
        session_id=session,
        seq=seq,
        pts=seq / 10,
    )


class Clock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


def test_a_subscriber_receives_frames_in_order():
    bus = FrameBus("cam")
    sub = bus.subscribe("t1", maxsize=8)
    for seq in range(3):
        bus.publish(_frame(seq))
    assert [sub.get_nowait().seq for _ in range(3)] == [0, 1, 2]
    assert sub.get_nowait() is None


def test_a_full_subscription_drops_its_oldest_frame_and_counts_it():
    bus = FrameBus("cam")
    sub = bus.subscribe("slow", maxsize=2)
    for seq in range(5):
        bus.publish(_frame(seq))
    assert sub.dropped == 3
    assert [sub.get_nowait().seq, sub.get_nowait().seq] == [3, 4], "the newest frames are kept"
    assert sub.delivered == 2


def test_one_slow_subscriber_never_delays_another_or_the_publisher():
    bus = FrameBus("cam")
    slow = bus.subscribe("slow", maxsize=1)  # never read
    fast = bus.subscribe("fast", maxsize=1000)
    started = time.monotonic()
    for seq in range(500):
        bus.publish(_frame(seq))
    elapsed = time.monotonic() - started
    assert elapsed < 2.0, "publish must not block on a full subscription"
    assert fast.dropped == 0 and fast.queued == 500
    assert slow.dropped == 499 and slow.queued == 1


def test_a_consumer_blocked_in_get_is_woken_by_a_publish():
    bus = FrameBus("cam")
    sub = bus.subscribe("waiter")
    got = []
    t = threading.Thread(target=lambda: got.append(sub.get(timeout=5.0)))
    t.start()
    time.sleep(0.05)
    bus.publish(_frame(7))
    t.join(timeout=5.0)
    assert got and got[0].seq == 7


def test_get_returns_none_when_no_frame_arrives_in_time():
    sub = FrameBus("cam").subscribe("idle")
    assert sub.get(timeout=0.05) is None


def test_counters_per_subscription():
    clock = Clock()
    bus = FrameBus("cam", clock=clock)
    sub = bus.subscribe("t1", maxsize=1)
    assert sub.last_frame_age_s is None, "no frame delivered yet is not an age of zero"
    bus.publish(_frame(0))
    bus.publish(_frame(1))
    assert sub.get_nowait().seq == 1
    clock.now += 2.5
    counters = sub.counters()
    assert counters["name"] == "t1"
    assert counters["delivered"] == 1
    assert counters["dropped"] == 1
    assert counters["last_frame_age_s"] == pytest.approx(2.5)


def test_bus_counters_sum_the_subscriptions():
    bus = FrameBus("cam")
    a = bus.subscribe("a", maxsize=1)
    bus.subscribe("b", maxsize=1)
    for seq in range(3):
        bus.publish(_frame(seq))
    a.get_nowait()
    counters = bus.counters()
    assert counters["stream"] == "cam"
    assert counters["published"] == 3
    assert counters["dropped"] == 4
    assert {s["name"] for s in counters["subscriptions"]} == {"a", "b"}


def test_publishing_with_no_subscriber_is_capture_all_the_same():
    """VID-009: nothing attached is a normal state, not an error."""
    bus = FrameBus("cam")
    assert bus.publish(_frame(0)) == 0
    assert bus.published == 1 and bus.dropped == 0


def test_a_subscription_name_is_unique_per_bus():
    bus = FrameBus("cam")
    bus.subscribe("motion")
    with pytest.raises(ValueError, match="motion"):
        bus.subscribe("motion")


def test_maxsize_must_hold_at_least_one_frame():
    with pytest.raises(ValueError):
        FrameBus("cam").subscribe("t", maxsize=0)


def test_an_unsubscribed_consumer_stops_receiving():
    bus = FrameBus("cam")
    sub = bus.subscribe("gone")
    bus.unsubscribe("gone")
    assert bus.publish(_frame(0)) == 0
    assert sub.closed
    assert sub.get(timeout=0.01) is None


def test_closing_the_bus_wakes_and_closes_every_subscription():
    bus = FrameBus("cam")
    sub = bus.subscribe("waiter")
    got = []
    t = threading.Thread(target=lambda: got.append(sub.get(timeout=5.0)))
    t.start()
    time.sleep(0.05)
    bus.close()
    t.join(timeout=5.0)
    assert got == [None] and sub.closed
