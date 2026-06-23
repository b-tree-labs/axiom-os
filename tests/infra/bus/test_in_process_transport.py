# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tests for the in-process transport.

Behavior: persists every accepted event to JSONL via locked_append_jsonl,
keeps an attach/detach subscriber registry, and yields matching subscribers
in priority order on `iter_subscribers`.
"""

from __future__ import annotations

import json

import pytest

from axiom.infra.bus.in_process import InProcessTransport
from axiom.infra.bus.transport import BusTransport
from axiom.infra.bus.types import Event, Subscription


class TestInProcessTransportProtocol:
    def test_satisfies_bus_transport_protocol(self):
        t = InProcessTransport()
        assert isinstance(t, BusTransport)


class TestAccept:
    def test_accept_no_log_path(self):
        t = InProcessTransport()
        e = Event(subject="x", payload={"v": 1})
        # No exception when log_path is None.
        t.accept(e)
        assert t.durability_log_path() is None

    def test_accept_writes_jsonl(self, tmp_path):
        log = tmp_path / "events.jsonl"
        t = InProcessTransport(log_path=log)
        t.accept(Event(subject="a", payload={"v": 1}, source="unit"))
        t.accept(Event(subject="b", payload={"v": 2}, source="unit"))

        lines = log.read_text().strip().splitlines()
        assert len(lines) == 2
        e1 = json.loads(lines[0])
        assert e1["subject"] == "a"
        assert e1["payload"] == {"v": 1}
        assert e1["source"] == "unit"
        assert "topic" not in e1  # No legacy fields.

    def test_durability_log_path_returns_path(self, tmp_path):
        log = tmp_path / "events.jsonl"
        t = InProcessTransport(log_path=log)
        assert t.durability_log_path() == log


class TestSubscriberRegistry:
    def test_attach_and_iter_concrete_match(self):
        t = InProcessTransport()
        sub = Subscription(
            pattern="tool.post_invoke",
            handler=lambda s, p: None,
            is_async=False,
        )
        t.attach_subscriber(sub)

        matches = list(t.iter_subscribers("tool.post_invoke"))
        assert matches == [sub]

    def test_attach_and_iter_wildcard_match(self):
        t = InProcessTransport()
        sub_a = Subscription(pattern="tool.>", handler=lambda s, p: None, is_async=False)
        sub_b = Subscription(pattern="tool.*", handler=lambda s, p: None, is_async=False)
        t.attach_subscriber(sub_a)
        t.attach_subscriber(sub_b)

        # `tool.post_invoke` matches both.
        matches = list(t.iter_subscribers("tool.post_invoke"))
        assert set(matches) == {sub_a, sub_b}

        # `tool.foo.bar` matches only `tool.>`.
        matches2 = list(t.iter_subscribers("tool.foo.bar"))
        assert matches2 == [sub_a]

    def test_iter_returns_priority_order(self):
        t = InProcessTransport()
        low = Subscription(pattern=">", handler=lambda s, p: None, is_async=False, priority=10)
        mid = Subscription(pattern=">", handler=lambda s, p: None, is_async=False, priority=100)
        high = Subscription(pattern=">", handler=lambda s, p: None, is_async=False, priority=200)
        # Attach out of order.
        t.attach_subscriber(mid)
        t.attach_subscriber(high)
        t.attach_subscriber(low)

        matches = list(t.iter_subscribers("anything"))
        assert matches == [low, mid, high]  # ascending priority — lower first.

    def test_detach_removes_subscriber(self):
        t = InProcessTransport()
        sub = Subscription(pattern=">", handler=lambda s, p: None, is_async=False)
        t.attach_subscriber(sub)
        t.detach_subscriber(sub)

        matches = list(t.iter_subscribers("anything"))
        assert matches == []

    def test_detach_unknown_subscription_is_noop(self):
        t = InProcessTransport()
        sub = Subscription(pattern=">", handler=lambda s, p: None, is_async=False)
        # Detaching something never attached must not raise.
        t.detach_subscriber(sub)

    def test_invalid_pattern_rejected_at_attach(self):
        from axiom.infra.bus.subjects import InvalidSubjectError

        t = InProcessTransport()
        bad = Subscription(pattern="Tool.*", handler=lambda s, p: None, is_async=False)
        with pytest.raises(InvalidSubjectError):
            t.attach_subscriber(bad)


class TestIterPending:
    def test_in_process_yields_nothing(self):
        # The in-process transport delivers synchronously; no events queue.
        t = InProcessTransport()
        assert list(t.iter_pending()) == []
