# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""`EventBus.history` is a bounded ring, not a process-lifetime tape.

A long-lived process (a serving worker, an aggregated protocol server, a
scheduled runner, a long conversational session) publishes for hours or
days. An unbounded history retains one `Event` per publish forever, and
every `tool.post_invoke` payload holds the full tool result by reference,
so the retained bytes grow with traffic rather than with anything the
operator asked for.

These tests pin the retention contract: the newest `history_limit` events
survive, in publish order, on every append path, and the durable log that
`replay()` reads is untouched by the cap.
"""

from __future__ import annotations

import json

import pytest

from axiom.infra.bus import UNBOUNDED_HISTORY, Event, EventBus
from axiom.infra.bus.event_bus import DEFAULT_HISTORY_LIMIT


def _raiser(subject: str, payload: dict) -> None:
    raise RuntimeError("boom")


class TestDefaultIsBounded:
    """The default must bound. That is the whole point of the fix."""

    def test_default_limit_is_a_positive_int(self):
        assert isinstance(DEFAULT_HISTORY_LIMIT, int)
        assert DEFAULT_HISTORY_LIMIT > 0

    def test_new_bus_reports_the_default_limit(self):
        assert EventBus().history_limit == DEFAULT_HISTORY_LIMIT

    def test_default_bus_stops_growing_at_the_default_limit(self):
        bus = EventBus()
        for i in range(DEFAULT_HISTORY_LIMIT + 250):
            bus.publish("t.event", {"i": i})

        assert len(bus.history) == DEFAULT_HISTORY_LIMIT


class TestRetentionWindow:
    def test_over_the_cap_retains_exactly_the_cap(self):
        bus = EventBus(history_limit=10)
        for i in range(100):
            bus.publish("t.event", {"i": i})

        assert len(bus.history) == 10

    def test_over_the_cap_retains_the_newest_not_the_oldest(self):
        bus = EventBus(history_limit=5)
        for i in range(20):
            bus.publish("t.event", {"i": i})

        assert [e.payload["i"] for e in bus.history] == [15, 16, 17, 18, 19]

    def test_ordering_is_oldest_first_within_the_window(self):
        bus = EventBus(history_limit=4)
        for name in ("a", "b", "c", "d", "e", "f"):
            bus.publish(f"t.{name}", {})

        assert [e.subject for e in bus.history] == ["t.c", "t.d", "t.e", "t.f"]

    def test_under_the_cap_behaves_exactly_as_before(self):
        """A bus that never reaches the cap is event-for-event unchanged."""
        bus = EventBus(history_limit=50)
        published = [bus.publish(f"t.{i}", {"i": i}, source="unit") for i in range(9)]

        hist = bus.history
        assert len(hist) == 9
        # Same objects, same order, nothing copied or re-created.
        assert all(h is p for h, p in zip(hist, published, strict=True))

    def test_exactly_at_the_cap_keeps_everything(self):
        bus = EventBus(history_limit=6)
        for i in range(6):
            bus.publish("t.event", {"i": i})

        assert [e.payload["i"] for e in bus.history] == [0, 1, 2, 3, 4, 5]


class TestErrorEventPathIsBounded:
    """`bus.errors` events append to history on their own path.

    Bounding only `publish()` would leave a process with one failing
    subscriber leaking just as fast as before.
    """

    def test_error_events_are_trimmed_too(self):
        bus = EventBus(history_limit=4)
        bus.subscribe("t.>", _raiser, fail_mode="ignore")

        for i in range(10):
            bus.publish(f"t.{i}", {})

        hist = bus.history
        assert len(hist) == 4
        # Each publish appends the event then its bus.errors companion,
        # so the newest four are the last two publish/error pairs.
        assert [e.subject for e in hist] == ["t.8", "bus.errors", "t.9", "bus.errors"]

    def test_error_events_alone_cannot_exceed_the_cap(self):
        bus = EventBus(history_limit=3)
        bus.subscribe("t.>", _raiser, fail_mode="ignore")

        for i in range(50):
            bus.publish(f"t.{i}", {})

        assert len(bus.history) == 3
        assert bus.history[-1].subject == "bus.errors"


class TestHistoryIsASafeSnapshot:
    def test_history_returns_a_plain_list(self):
        bus = EventBus(history_limit=10)
        bus.publish("t.event", {})

        assert type(bus.history) is list

    def test_every_element_is_an_event(self):
        bus = EventBus(history_limit=10)
        bus.publish("t.event", {})

        assert all(isinstance(e, Event) for e in bus.history)

    def test_caller_mutation_cannot_corrupt_the_bus(self):
        bus = EventBus(history_limit=10)
        bus.publish("t.one", {})
        bus.publish("t.two", {})

        hist = bus.history
        hist.clear()
        hist.append(Event(subject="t.injected", payload={}))

        assert [e.subject for e in bus.history] == ["t.one", "t.two"]

    def test_each_read_returns_a_fresh_list(self):
        bus = EventBus(history_limit=10)
        bus.publish("t.event", {})

        assert bus.history is not bus.history


class TestExplicitLimit:
    def test_explicit_cap_is_honoured(self):
        bus = EventBus(history_limit=3)
        for i in range(7):
            bus.publish("t.event", {"i": i})

        assert bus.history_limit == 3
        assert [e.payload["i"] for e in bus.history] == [4, 5, 6]

    def test_zero_keeps_nothing(self):
        bus = EventBus(history_limit=0)
        bus.publish("t.event", {})

        assert bus.history == []

    def test_negative_is_rejected(self):
        with pytest.raises(ValueError, match="history_limit"):
            EventBus(history_limit=-1)

    def test_limit_is_compatible_with_a_durable_log(self, tmp_path):
        bus = EventBus(log_path=tmp_path / "events.jsonl", history_limit=2)
        for i in range(5):
            bus.publish("t.event", {"i": i})

        assert len(bus.history) == 2


class TestUnboundedIsOptIn:
    def test_unbounded_sentinel_disables_the_cap(self):
        bus = EventBus(history_limit=UNBOUNDED_HISTORY)
        for i in range(DEFAULT_HISTORY_LIMIT + 25):
            bus.publish("t.event", {"i": i})

        assert bus.history_limit is None
        assert len(bus.history) == DEFAULT_HISTORY_LIMIT + 25

    def test_unbounded_is_never_the_default(self):
        assert UNBOUNDED_HISTORY is None
        assert EventBus().history_limit is not None


class TestMemoryShape:
    """The test that would have caught the original leak.

    Retained count must stay flat as the publish count grows. Asserting a
    ceiling only would pass on a list that grows to just under it, so this
    checks the retained count is identical at 2x, 5x and 10x the cap.
    """

    def test_retained_count_stays_flat_as_publish_count_grows(self):
        cap = 25
        bus = EventBus(history_limit=cap)
        published = 0
        retained: list[int] = []

        for multiple in (2, 5, 10):
            while published < cap * multiple:
                bus.publish("t.event", {"n": published})
                published += 1
            retained.append(len(bus.history))

        assert published == cap * 10
        assert retained == [cap, cap, cap]

    def test_unbounded_history_grows_with_traffic(self):
        """The leak, reproduced deliberately, so the contrast is on record."""
        bus = EventBus(history_limit=UNBOUNDED_HISTORY)
        counts = []
        for _ in range(3):
            for _ in range(40):
                bus.publish("t.event", {})
            counts.append(len(bus.history))

        assert counts == [40, 80, 120]


class TestReplayIsUnaffected:
    """`replay()` reads the durable log, not `_history`. Capping one must
    not cap the other."""

    def test_replay_returns_every_logged_event_past_the_cap(self, tmp_path):
        log = tmp_path / "events.jsonl"
        writer = EventBus(log_path=log, history_limit=3)
        for i in range(30):
            writer.publish("t.event", {"i": i})

        reader = EventBus(log_path=log, history_limit=3)
        seen: list[int] = []
        reader.subscribe("t.>", lambda s, p: seen.append(p["i"]))
        replayed = reader.replay()

        assert len(replayed) == 30
        assert seen == list(range(30))
        assert len(log.read_text(encoding="utf-8").strip().splitlines()) == 30

    def test_durable_log_still_holds_evicted_events(self, tmp_path):
        log = tmp_path / "events.jsonl"
        bus = EventBus(log_path=log, history_limit=2)
        for i in range(6):
            bus.publish("t.event", {"i": i})

        logged = [
            json.loads(line)["payload"]["i"]
            for line in log.read_text(encoding="utf-8").strip().splitlines()
        ]
        assert logged == [0, 1, 2, 3, 4, 5]
        assert [e.payload["i"] for e in bus.history] == [4, 5]
