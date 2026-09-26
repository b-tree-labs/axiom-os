# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tests for connector-status observability + reconnect surface.

Pins:

- ``publish_outcome`` emits the right subject per result shape
- ``publish_outcome`` is resilient — never raises (bus None / bus throws)
- ``InMemoryStatusStore`` round-trip + latest + history + reconnect_pending
- ``StatusStoreSubscriber.attach()`` wires the bus
- Bus events → store rows via the subscriber
- ``connector_status`` skill returns table shape
- ``connector_reconnect`` skill returns actionable next-action
- Skills handle empty / unknown connector cleanly
- Module-level default-store singleton + test reset
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

import pytest

from axiom.extensions.builtins.connector.observability import (
    SUBJECT_DELIVERED,
    SUBJECT_FAILED,
    SUBJECT_RECONNECT_REQUIRED,
    ConnectorOutcome,
    publish_outcome,
)
from axiom.extensions.builtins.connector.skills.reconnect import (
    run as reconnect_run,
)
from axiom.extensions.builtins.connector.skills.status import (
    run as status_run,
)
from axiom.extensions.builtins.connector.status_store import (
    InMemoryStatusStore,
    StatusStoreSubscriber,
    get_default_store,
    reset_default_store_for_testing,
)
from axiom.infra.bus import EventBus
from axiom.infra.bus.in_process import InProcessTransport


@pytest.fixture(autouse=True)
def _reset_default_store():
    reset_default_store_for_testing()
    yield
    reset_default_store_for_testing()


# ---------------------------------------------------------------------------
# Stub result shapes — match the duck-type adapters return
# ---------------------------------------------------------------------------


@dataclass
class _OkResult:
    ok: bool = True
    status_code: int = 200
    retry_attempts: int = 0
    reconnect_required: bool = False
    error: str | None = None
    message_id: str | None = "msg-1"


@dataclass
class _FailResult:
    ok: bool = False
    status_code: int = 502
    error: str = "HTTP 502: bad gateway"
    retry_attempts: int = 2
    reconnect_required: bool = False
    message_id: str | None = None


@dataclass
class _AuthFailResult:
    ok: bool = False
    status_code: int = 401
    error: str = "HTTP 401 (auth)"
    retry_attempts: int = 0
    reconnect_required: bool = True
    message_id: str | None = None


# ---------------------------------------------------------------------------
# Stub bus — captures publishes
# ---------------------------------------------------------------------------


class _CapturingBus:
    def __init__(self):
        self.published: list[tuple[str, dict, str]] = []

    def publish(self, subject, payload, source=""):
        self.published.append((subject, payload, source))


# ---------------------------------------------------------------------------
# publish_outcome
# ---------------------------------------------------------------------------


class TestPublishOutcome:
    def test_success_routes_to_delivered(self):
        bus = _CapturingBus()
        publish_outcome(
            bus, connector="slack", result=_OkResult(), recipient="#x", receipt_id="r1"
        )
        assert len(bus.published) == 1
        subject, payload, source = bus.published[0]
        assert subject == SUBJECT_DELIVERED
        assert payload["connector"] == "slack"
        assert payload["ok"] is True
        assert payload["recipient"] == "#x"
        assert payload["receipt_id"] == "r1"
        assert payload["observed_at"]  # ISO-ish; presence is enough here
        assert source == "connector.slack"

    def test_non_auth_failure_routes_to_failed(self):
        bus = _CapturingBus()
        publish_outcome(bus, connector="teams", result=_FailResult())
        assert bus.published[0][0] == SUBJECT_FAILED

    def test_auth_failure_routes_to_reconnect_required(self):
        bus = _CapturingBus()
        publish_outcome(bus, connector="teams", result=_AuthFailResult())
        subject, payload, _ = bus.published[0]
        assert subject == SUBJECT_RECONNECT_REQUIRED
        assert payload["reconnect_required"] is True
        assert payload["status_code"] == 401

    def test_resilient_when_bus_none(self):
        # Must not raise — observability never breaks the caller.
        publish_outcome(None, connector="x", result=_OkResult())

    def test_resilient_when_bus_throws(self):
        class _BadBus:
            def publish(self, *a, **kw):
                raise RuntimeError("bus down")

        # Must not raise.
        publish_outcome(_BadBus(), connector="x", result=_OkResult())


# ---------------------------------------------------------------------------
# Outcome dataclass round-trip
# ---------------------------------------------------------------------------


class TestOutcomeRoundTrip:
    def test_from_payload_parses_iso(self):
        ts = datetime.now(timezone.utc).isoformat()
        outcome = ConnectorOutcome.from_payload({
            "connector": "slack",
            "ok": True,
            "observed_at": ts,
            "recipient": "#x",
            "status_code": 200,
        })
        assert outcome.connector == "slack"
        assert outcome.observed_at.isoformat() == ts

    def test_from_payload_with_bad_ts_does_not_crash(self):
        outcome = ConnectorOutcome.from_payload({
            "connector": "slack", "ok": True, "observed_at": "garbage"
        })
        # Falls back to "now" rather than raising.
        assert isinstance(outcome.observed_at, datetime)


# ---------------------------------------------------------------------------
# InMemoryStatusStore
# ---------------------------------------------------------------------------


def _outcome(connector="slack", ok=True, reconnect=False) -> ConnectorOutcome:
    return ConnectorOutcome(
        connector=connector,
        ok=ok,
        observed_at=datetime.now(timezone.utc),
        reconnect_required=reconnect,
    )


class TestInMemoryStore:
    def test_record_and_latest(self):
        s = InMemoryStatusStore()
        s.record(_outcome("slack", ok=True))
        assert s.latest("slack").ok is True

    def test_latest_overwrites_per_connector(self):
        s = InMemoryStatusStore()
        s.record(_outcome("slack", ok=False))
        s.record(_outcome("slack", ok=True))
        assert s.latest("slack").ok is True

    def test_all_latest_includes_every_connector(self):
        s = InMemoryStatusStore()
        s.record(_outcome("slack"))
        s.record(_outcome("teams"))
        assert set(s.all_latest()) == {"slack", "teams"}

    def test_reconnect_pending_filters_correctly(self):
        s = InMemoryStatusStore()
        s.record(_outcome("slack", reconnect=False))
        s.record(_outcome("teams", reconnect=True))
        s.record(_outcome("email", reconnect=True))
        pending = {o.connector for o in s.reconnect_pending()}
        assert pending == {"teams", "email"}

    def test_history_returns_recent_outcomes(self):
        s = InMemoryStatusStore(history_per_connector=3)
        for _ in range(5):
            s.record(_outcome("slack"))
        assert len(s.history("slack")) == 3  # rolling buffer cap

    def test_history_unknown_connector_returns_empty(self):
        s = InMemoryStatusStore()
        assert s.history("nope") == []


# ---------------------------------------------------------------------------
# Subscriber + bus integration
# ---------------------------------------------------------------------------


class TestSubscriberIntegration:
    def _bus(self):
        return EventBus(transport=InProcessTransport())

    def test_attach_subscribes_three_subjects(self):
        store = InMemoryStatusStore()
        sub = StatusStoreSubscriber(store)
        sub.attach(self._bus())
        assert len(sub.subscriptions) == 3

    def test_detach_unsubscribes(self):
        store = InMemoryStatusStore()
        sub = StatusStoreSubscriber(store)
        sub.attach(self._bus())
        sub.detach()
        assert sub.subscriptions == []

    def test_end_to_end_publish_to_store(self):
        """Adapter publishes → bus dispatches → store records."""
        store = InMemoryStatusStore()
        sub = StatusStoreSubscriber(store)
        bus = self._bus()
        sub.attach(bus)

        publish_outcome(
            bus, connector="slack", result=_OkResult(), recipient="#x"
        )
        publish_outcome(
            bus, connector="teams", result=_AuthFailResult()
        )

        assert store.latest("slack").ok is True
        assert store.latest("teams").reconnect_required is True
        pending = {o.connector for o in store.reconnect_pending()}
        assert pending == {"teams"}


# ---------------------------------------------------------------------------
# Skills — connector_status
# ---------------------------------------------------------------------------


class TestConnectorStatusSkill:
    def test_empty_store(self):
        result = status_run({"store": InMemoryStatusStore()}, ctx=None)
        assert result.ok is True
        assert result.value["count"] == 0
        assert result.value["reconnect_pending_count"] == 0

    def test_lists_every_connector(self):
        store = InMemoryStatusStore()
        store.record(_outcome("slack"))
        store.record(_outcome("teams", reconnect=True))
        result = status_run({"store": store}, ctx=None)
        names = {c["connector"] for c in result.value["connectors"]}
        assert names == {"slack", "teams"}
        assert result.value["reconnect_pending_count"] == 1
        assert result.value["reconnect_pending"][0]["connector"] == "teams"

    def test_filter_by_one_connector(self):
        store = InMemoryStatusStore()
        store.record(_outcome("slack", ok=False))
        result = status_run(
            {"store": store, "connector": "slack"}, ctx=None
        )
        assert result.value["found"] is True
        assert result.value["outcome"]["connector"] == "slack"

    def test_filter_unknown_connector(self):
        result = status_run(
            {"store": InMemoryStatusStore(), "connector": "ghost"}, ctx=None
        )
        assert result.value["found"] is False
        assert "ghost" in result.value["note"]

    def test_uses_default_store_when_none_passed(self):
        # Default store should be reachable; not error.
        result = status_run({}, ctx=None)
        assert result.ok is True

    def test_default_store_singleton(self):
        s1 = get_default_store()
        s2 = get_default_store()
        assert s1 is s2


# ---------------------------------------------------------------------------
# Skills — connector_reconnect
# ---------------------------------------------------------------------------


class TestConnectorReconnectSkill:
    def test_no_pending_returns_empty_note(self):
        result = reconnect_run({"store": InMemoryStatusStore()}, ctx=None)
        assert result.ok is True
        assert result.value["reconnect_pending_count"] == 0

    def test_pending_returns_action_rows(self):
        store = InMemoryStatusStore()
        store.record(_outcome("teams", reconnect=True))
        result = reconnect_run({"store": store}, ctx=None)
        assert result.value["reconnect_pending_count"] == 1
        row = result.value["pending"][0]
        assert row["connector"] == "teams"
        assert "axi notifications connector add teams" in row["next_action"]
        assert "--reconnect" in row["next_action"]

    def test_named_unknown_connector_returns_error(self):
        result = reconnect_run(
            {"store": InMemoryStatusStore(), "connector": "ghost"},
            ctx=None,
        )
        assert result.ok is False
        assert any("ghost" in e for e in result.errors)

    def test_named_healthy_connector_returns_no_action(self):
        store = InMemoryStatusStore()
        store.record(_outcome("slack", ok=True))
        result = reconnect_run(
            {"store": store, "connector": "slack"}, ctx=None
        )
        assert result.value["needs_reconnect"] is False

    def test_named_failing_connector_returns_action(self):
        store = InMemoryStatusStore()
        store.record(_outcome("teams", reconnect=True))
        result = reconnect_run(
            {"store": store, "connector": "teams"}, ctx=None
        )
        assert result.value["needs_reconnect"] is True
        assert "axi notifications connector add teams" in (
            result.value["next_action"]
        )
