# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tests for the synchronous EventBus surface.

Covers the v1 semantics that consumers depend on (publish/subscribe,
priority ordering, history, replay, durable JSONL log) under the new
NATS-shape subject grammar.
"""

from __future__ import annotations

import json

import pytest

from axiom.infra.bus import Event, EventBus
from axiom.infra.bus.subjects import InvalidSubjectError


class TestSubscribeAndPublish:
    def test_concrete_subject_match(self):
        bus = EventBus()
        received = []
        bus.subscribe("test.hello", lambda s, p: received.append((s, p)))

        bus.publish("test.hello", {"msg": "hi"})

        assert received == [("test.hello", {"msg": "hi"})]

    def test_wildcard_star_match(self):
        bus = EventBus()
        received = []
        bus.subscribe("sense.*", lambda s, p: received.append(s))

        bus.publish("sense.ingest_complete", {})
        bus.publish("sense.draft_ready", {})
        bus.publish("doc.publish_complete", {})  # No match.

        assert received == ["sense.ingest_complete", "sense.draft_ready"]

    def test_gt_matches_all(self):
        bus = EventBus()
        received = []
        bus.subscribe(">", lambda s, p: received.append(s))

        bus.publish("a", {})
        bus.publish("b.c", {})
        bus.publish("d.e.f", {})

        assert received == ["a", "b.c", "d.e.f"]

    def test_publish_returns_event(self):
        bus = EventBus()
        e = bus.publish("test", {"x": 1}, source="unit")
        assert isinstance(e, Event)
        assert e.subject == "test"
        assert e.payload == {"x": 1}
        assert e.source == "unit"

    def test_publish_default_payload(self):
        bus = EventBus()
        received = []
        bus.subscribe(">", lambda s, p: received.append(p))
        bus.publish("test")
        assert received == [{}]


class TestUnsubscribe:
    def test_unsubscribe_removes_handler(self):
        bus = EventBus()
        received = []

        def handler(s, p):
            received.append(s)

        sub = bus.subscribe(">", handler)
        bus.publish("first", {})
        bus.unsubscribe(sub)
        bus.publish("second", {})

        assert received == ["first"]


class TestPriority:
    def test_lower_priority_runs_first(self):
        bus = EventBus()
        order = []
        bus.subscribe(">", lambda s, p: order.append("mid"), priority=100)
        bus.subscribe(">", lambda s, p: order.append("high"), priority=200)
        bus.subscribe(">", lambda s, p: order.append("low"), priority=10)

        bus.publish("test", {})

        assert order == ["low", "mid", "high"]


class TestInvalidSubject:
    def test_subscribe_rejects_bad_pattern(self):
        bus = EventBus()
        with pytest.raises(InvalidSubjectError):
            bus.subscribe("Tool.*", lambda s, p: None)

    def test_publish_rejects_bad_subject(self):
        bus = EventBus()
        with pytest.raises(InvalidSubjectError):
            bus.publish("Tool.PostInvoke", {})


class TestHistory:
    def test_history_records_published_events(self):
        bus = EventBus()
        bus.publish("a", {"x": 1})
        bus.publish("b", {"y": 2})

        hist = bus.history
        assert len(hist) == 2
        assert hist[0].subject == "a"
        assert hist[1].subject == "b"


class TestDurableLog:
    def test_log_written(self, tmp_path):
        log = tmp_path / "events.jsonl"
        bus = EventBus(log_path=log)

        bus.publish("sense.ingest", {"count": 5})
        bus.publish("doc.publish", {"url": "http://example.com"})

        lines = log.read_text().strip().splitlines()
        assert len(lines) == 2
        e1 = json.loads(lines[0])
        assert e1["subject"] == "sense.ingest"
        assert e1["payload"]["count"] == 5


class TestReplay:
    def test_replay_dispatches_logged_events(self, tmp_path):
        log = tmp_path / "events.jsonl"
        bus1 = EventBus(log_path=log)
        bus1.publish("a", {"v": 1})
        bus1.publish("b", {"v": 2})

        # New bus replays from log.
        bus2 = EventBus(log_path=log)
        replayed = []
        bus2.subscribe(">", lambda s, p: replayed.append(s))
        events = bus2.replay()

        assert len(events) == 2
        assert replayed == ["a", "b"]

    def test_replay_with_since_filter(self, tmp_path):
        log = tmp_path / "events.jsonl"
        with open(log, "w") as f:
            f.write(
                json.dumps(
                    {"subject": "old", "payload": {}, "timestamp": "2026-01-01T00:00:00"},
                )
                + "\n",
            )
            f.write(
                json.dumps(
                    {"subject": "new", "payload": {}, "timestamp": "2026-02-18T00:00:00"},
                )
                + "\n",
            )

        bus = EventBus(log_path=log)
        replayed = []
        bus.subscribe(">", lambda s, p: replayed.append(s))
        bus.replay(since="2026-02-01T00:00:00")

        assert replayed == ["new"]


class TestSubscribersFor:
    def test_diagnostic_lookup(self):
        bus = EventBus()
        sub_a = bus.subscribe("tool.>", lambda s, p: None)
        sub_b = bus.subscribe("tool.post_invoke", lambda s, p: None)
        bus.subscribe("session.>", lambda s, p: None)

        matches = bus.subscribers_for("tool.post_invoke")
        assert set(matches) == {sub_a, sub_b}
