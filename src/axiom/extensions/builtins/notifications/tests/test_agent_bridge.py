# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tests for the agent-bus → HERALD bridge.

Pins the contract that lifecycle events emitted by RIVET / TIDY / SCAN /
any agent on the default EventBus flow through ``notifications.send()``
to the configured channels.

Reuses what's already shipped — the existing ``EventBus`` + ``send()``
façade + ``ChannelAdapterRegistry`` — without adding any new persistence
or duplicating routing logic. The bridge is the missing wire, not a new
fabric.
"""

from __future__ import annotations

from axiom.extensions.builtins.notifications.agent_bridge import (
    AgentBridge,
    BridgeRouting,
    BridgeRule,
    default_routing,
)
from axiom.extensions.builtins.notifications.send import (
    Priority,
    SendContext,
)
from axiom.governance import Classification
from axiom.infra.bus import EventBus
from axiom.infra.bus.in_process import InProcessTransport

# ---------------------------------------------------------------------------
# Test scaffolding
# ---------------------------------------------------------------------------


def _bus() -> EventBus:
    """Fresh in-process EventBus per test."""
    return EventBus(transport=InProcessTransport())


def _ctx() -> SendContext:
    """Default send-context — inbox only (the SEC-1 baseline). Adding
    slack/teams/etc. providers happens through registry.register at
    deployment time; the bridge doesn't care which channels are present."""
    return SendContext.default()


def _record_sends(monkeypatch) -> list[dict]:
    """Patch ``send()`` from the bridge module's import site so we can
    assert calls without exercising the full receipt pipeline."""
    captured: list[dict] = []

    def _fake_send(ctx, *, actor, recipient, payload, classification,
                   priority, intent, channel_prefs=None, dedup_key=None):
        captured.append({
            "actor": actor,
            "recipient": recipient,
            "payload": payload,
            "classification": classification,
            "priority": priority,
            "intent": intent,
            "dedup_key": dedup_key,
        })
        # Tests don't assert on the receipt — return a stub.
        from axiom.extensions.builtins.notifications.send import (
            DeliveryReceipt,
        )
        return DeliveryReceipt(
            id="rcpt-stub",
            actor=actor,
            recipient=recipient,
            intent=intent,
        )

    import axiom.extensions.builtins.notifications.agent_bridge as mod
    monkeypatch.setattr(mod, "send", _fake_send)
    return captured


# ---------------------------------------------------------------------------
# BridgeRule + BridgeRouting
# ---------------------------------------------------------------------------


class TestRouting:
    def test_default_routing_includes_rivet_lifecycle(self):
        rules = default_routing().rules
        subjects = {r.subject_pattern for r in rules}
        # Per RIVET's lifecycle-event emission (release/lifecycle_events.py).
        assert any(p.startswith("rivet.") for p in subjects)

    def test_default_routing_includes_tidy_signals(self):
        rules = default_routing().rules
        subjects = {r.subject_pattern for r in rules}
        assert any(p.startswith("tidy.") for p in subjects)

    def test_default_routing_includes_escalation_wildcard(self):
        # Any agent's escalation event should route — broad pattern.
        rules = default_routing().rules
        subjects = {r.subject_pattern for r in rules}
        assert "*.escalation" in subjects or any(
            ".escalation" in p for p in subjects
        )

    def test_first_match_wins_when_overlapping(self):
        # Specific rule before wildcard: specific wins.
        routing = BridgeRouting(rules=[
            BridgeRule(
                subject_pattern="rivet.ci_recovered",
                summary_template="CI recovered: {repo}",
                priority=Priority.NORMAL,
                classification=Classification.INTERNAL,
            ),
            BridgeRule(
                subject_pattern="rivet.*",
                summary_template="generic rivet event",
                priority=Priority.LOW,
                classification=Classification.INTERNAL,
            ),
        ])
        match = routing.find("rivet.ci_recovered")
        assert match is not None
        assert match.summary_template == "CI recovered: {repo}"


# ---------------------------------------------------------------------------
# Subscribe / detach lifecycle
# ---------------------------------------------------------------------------


class TestAttachDetach:
    def test_attach_subscribes_each_rule(self):
        bus = _bus()
        routing = BridgeRouting(rules=[
            BridgeRule(subject_pattern="rivet.pr_merged"),
            BridgeRule(subject_pattern="tidy.escalation"),
        ])
        bridge = AgentBridge(send_ctx=_ctx(), routing=routing)
        bridge.attach(bus)
        # Two rules → two subscriptions.
        assert len(bridge.subscriptions) == 2

    def test_detach_unsubscribes(self):
        bus = _bus()
        routing = BridgeRouting(rules=[
            BridgeRule(subject_pattern="rivet.pr_merged"),
        ])
        bridge = AgentBridge(send_ctx=_ctx(), routing=routing)
        bridge.attach(bus)
        bridge.detach()
        assert bridge.subscriptions == []


# ---------------------------------------------------------------------------
# Event → send() routing
# ---------------------------------------------------------------------------


class TestEventToSend:
    def test_rivet_ci_recovered_routes_to_send(self, monkeypatch):
        captured = _record_sends(monkeypatch)
        bus = _bus()
        routing = BridgeRouting(rules=[
            BridgeRule(
                subject_pattern="rivet.ci_recovered",
                summary_template="CI recovered on {repo}",
                priority=Priority.NORMAL,
                classification=Classification.INTERNAL,
                actor="@rivet",
                recipient="@operator",
            ),
        ])
        bridge = AgentBridge(send_ctx=_ctx(), routing=routing)
        bridge.attach(bus)
        bus.publish(
            "rivet.ci_recovered",
            {"repo": "b-tree-labs/axiom-os"},
            source="rivet",
        )
        assert len(captured) == 1
        call = captured[0]
        assert call["actor"] == "@rivet"
        assert call["recipient"] == "@operator"
        assert call["priority"] == Priority.NORMAL
        assert call["classification"] == Classification.INTERNAL
        assert call["payload"].summary == "CI recovered on b-tree-labs/axiom-os"

    def test_summary_template_with_missing_field_does_not_crash(
        self, monkeypatch
    ):
        # If RIVET emits a payload missing the templated field, we degrade
        # gracefully (summary falls back to the raw subject).
        captured = _record_sends(monkeypatch)
        bus = _bus()
        routing = BridgeRouting(rules=[
            BridgeRule(
                subject_pattern="rivet.pr_merged",
                summary_template="merged PR #{pr_number}",
            ),
        ])
        AgentBridge(send_ctx=_ctx(), routing=routing).attach(bus)
        bus.publish("rivet.pr_merged", {}, source="rivet")
        assert len(captured) == 1
        # Bridge didn't raise; summary mentions the subject.
        assert "rivet.pr_merged" in captured[0]["payload"].summary

    def test_unmatched_subject_is_ignored(self, monkeypatch):
        captured = _record_sends(monkeypatch)
        bus = _bus()
        routing = BridgeRouting(rules=[
            BridgeRule(subject_pattern="rivet.ci_recovered"),
        ])
        AgentBridge(send_ctx=_ctx(), routing=routing).attach(bus)
        # Subscribed only to rivet.ci_recovered — different subject must
        # not be routed.
        bus.publish("tidy.escalation", {"x": 1}, source="tidy")
        assert captured == []

    def test_wildcard_pattern_matches_subtree(self, monkeypatch):
        captured = _record_sends(monkeypatch)
        bus = _bus()
        routing = BridgeRouting(rules=[
            BridgeRule(
                subject_pattern="rivet.*",
                summary_template="rivet event: {_subject}",
                priority=Priority.LOW,
            ),
        ])
        AgentBridge(send_ctx=_ctx(), routing=routing).attach(bus)
        bus.publish("rivet.pr_merged", {"pr": 1}, source="rivet")
        bus.publish("rivet.tag_released", {"tag": "v1"}, source="rivet")
        assert len(captured) == 2
        assert all(c["priority"] == Priority.LOW for c in captured)

    def test_subject_string_available_to_template(self, monkeypatch):
        # `_subject` is a reserved template var so rules don't need to
        # duplicate the subject in every payload.
        captured = _record_sends(monkeypatch)
        bus = _bus()
        routing = BridgeRouting(rules=[
            BridgeRule(
                subject_pattern="*.escalation",
                summary_template="{_subject}: {detail}",
            ),
        ])
        AgentBridge(send_ctx=_ctx(), routing=routing).attach(bus)
        bus.publish(
            "tidy.escalation",
            {"detail": "disk pressure critical"},
            source="tidy",
        )
        assert captured[0]["payload"].summary == (
            "tidy.escalation: disk pressure critical"
        )

    def test_dedup_key_derived_from_subject_plus_payload(self, monkeypatch):
        # Bridge derives a stable dedup_key so duplicate-published events
        # collapse to one delivery per fabric §6.1.
        captured = _record_sends(monkeypatch)
        bus = _bus()
        routing = BridgeRouting(rules=[
            BridgeRule(subject_pattern="rivet.pr_merged"),
        ])
        AgentBridge(send_ctx=_ctx(), routing=routing).attach(bus)
        bus.publish(
            "rivet.pr_merged", {"pr_number": 42}, source="rivet"
        )
        assert captured[0]["dedup_key"] is not None
        # Same subject + payload → same dedup_key when re-published.
        captured.clear()
        bus.publish(
            "rivet.pr_merged", {"pr_number": 42}, source="rivet"
        )
        bus.publish(
            "rivet.pr_merged", {"pr_number": 43}, source="rivet"
        )
        # Two events, two send calls (dedup is enforced by send(), not
        # the bridge — bridge just supplies stable keys).
        assert len(captured) == 2
        assert captured[0]["dedup_key"] != captured[1]["dedup_key"]


# ---------------------------------------------------------------------------
# Error isolation
# ---------------------------------------------------------------------------


class TestErrorIsolation:
    def test_send_exception_does_not_break_bus(self, monkeypatch):
        bus = _bus()

        def boom(*args, **kwargs):
            raise RuntimeError("downstream failure")

        import axiom.extensions.builtins.notifications.agent_bridge as mod
        monkeypatch.setattr(mod, "send", boom)

        routing = BridgeRouting(rules=[
            BridgeRule(subject_pattern="rivet.pr_merged"),
        ])
        AgentBridge(send_ctx=_ctx(), routing=routing).attach(bus)
        # Publish must not raise — bridge swallows downstream failures so
        # the agent emitting the signal isn't punished for HERALD's bad day.
        bus.publish("rivet.pr_merged", {"pr_number": 1}, source="rivet")


# ---------------------------------------------------------------------------
# v0.30 / ADR-060 — publishing.* and rivet.notification routing
# ---------------------------------------------------------------------------


class TestM3PublishingRoutes:
    def test_default_routing_includes_publishing_events(self):
        rules = default_routing().rules
        subjects = {r.subject_pattern for r in rules}
        assert "publishing.succeeded" in subjects
        assert "publishing.draft_ready" in subjects
        assert "publishing.failed" in subjects

    def test_default_routing_includes_rivet_notification(self):
        rules = default_routing().rules
        subjects = {r.subject_pattern for r in rules}
        assert "rivet.notification" in subjects

    def test_publishing_succeeded_routes_to_send(self, monkeypatch):
        captured = _record_sends(monkeypatch)
        bus = _bus()
        AgentBridge(send_ctx=_ctx()).attach(bus)
        bus.publish(
            "publishing.succeeded",
            {"source": "/tmp/doc.md"},
            source="agent.press",
        )
        assert len(captured) == 1
        # Was routed under press actor.
        assert captured[0]["actor"] == "@press"
        assert "/tmp/doc.md" in captured[0]["payload"].summary
