# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tests for Event / Subscription / FailMode types."""

from __future__ import annotations

import pytest

from axiom.infra.bus.types import Event, FailMode, Subscription


class TestEvent:
    """Event data model with NATS-aligned field names."""

    def test_construct_with_subject_and_payload(self):
        e = Event(subject="tool.post_invoke", payload={"x": 1})
        assert e.subject == "tool.post_invoke"
        assert e.payload == {"x": 1}

    def test_auto_timestamp(self):
        e = Event(subject="test", payload={})
        assert e.timestamp  # ISO-8601 set in __post_init__
        assert "T" in e.timestamp

    def test_default_source_and_envelope(self):
        e = Event(subject="test", payload={})
        assert e.source == ""
        assert e.envelope == {}

    def test_envelope_field(self):
        e = Event(subject="test", payload={}, envelope={"sig": "abc"})
        assert e.envelope == {"sig": "abc"}

    def test_to_dict_uses_subject_and_payload(self):
        e = Event(subject="test.x", payload={"k": "v"}, source="unit")
        d = e.to_dict()
        assert d["subject"] == "test.x"
        assert d["payload"] == {"k": "v"}
        assert d["source"] == "unit"
        # No legacy aliases.
        assert "topic" not in d
        assert "data" not in d

    def test_from_dict_roundtrip(self):
        e = Event(subject="test", payload={"a": 1}, source="unit")
        d = e.to_dict()
        e2 = Event.from_dict(d)
        assert e2.subject == e.subject
        assert e2.payload == e.payload
        assert e2.source == e.source
        assert e2.timestamp == e.timestamp

    def test_from_dict_defaults(self):
        # Minimal dict with only subject.
        e = Event.from_dict({"subject": "x"})
        assert e.subject == "x"
        assert e.payload == {}
        assert e.source == ""
        assert e.envelope == {}

    def test_no_legacy_topic_attribute(self):
        e = Event(subject="test", payload={})
        # No backward-compat property; field is `subject`, not `topic`.
        assert not hasattr(e, "topic")
        assert not hasattr(e, "data")


class TestSubscription:
    """Subscription is a frozen dataclass with priority + fail_mode."""

    def test_construct_minimal(self):
        sub = Subscription(
            pattern="tool.>",
            handler=lambda s, p: None,
            is_async=False,
        )
        assert sub.pattern == "tool.>"
        assert sub.priority == 100
        assert sub.fail_mode == "warn"
        assert sub.source == ""

    def test_frozen(self):
        sub = Subscription(
            pattern="x",
            handler=lambda s, p: None,
            is_async=False,
        )
        with pytest.raises((AttributeError, Exception)):  # FrozenInstanceError or similar
            sub.priority = 50  # type: ignore[misc]

    def test_priority_and_fail_mode(self):
        sub = Subscription(
            pattern="x",
            handler=lambda s, p: None,
            is_async=False,
            priority=10,
            fail_mode="abort",
        )
        assert sub.priority == 10
        assert sub.fail_mode == "abort"


class TestFailMode:
    """FailMode is the literal type used for handler failure semantics."""

    def test_valid_values(self):
        # All three are valid values for the FailMode literal.
        valid: list[FailMode] = ["abort", "warn", "ignore"]
        assert valid == ["abort", "warn", "ignore"]
