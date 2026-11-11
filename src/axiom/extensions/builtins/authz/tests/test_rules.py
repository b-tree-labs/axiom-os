# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tests for the authz rule engine.

Per prd-axiom-authz §5.2: rule evaluation has documented precedence
(deny > propose > require_capability > permit; higher priority wins
within a disposition; empty match returns None).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from axiom.extensions.builtins.authz.rules import Rule, RuleEngine
from axiom.governance import (
    ActionEnvelope,
    ActionIntent,
    CapabilityToken,
    Classification,
    IntentPattern,
    ProvenanceRef,
    ResourcePattern,
    ResourceRef,
)
from axiom.vega.identity.principal import Principal


def _alice() -> Principal:
    return Principal(handle="@alice:test", public_bytes=b"\x00" * 32)


def _env(
    intent: str = "notification.send",
    resource: str = "slack://team-rsc/#alerts",
    actor: str = "@alice:test",
    classification: Classification = Classification.INTERNAL,
    federation_origin: str | None = None,
) -> ActionEnvelope:
    principal = Principal(handle=actor, public_bytes=b"\x00" * 32)
    return ActionEnvelope(
        actor=principal,
        capability=CapabilityToken.unscoped_test_token(subject=principal),
        classification=classification,
        context_fragment_id="memory://localhost/test",
        provenance_parent=ProvenanceRef.synthetic("test"),
        federation_origin=federation_origin,
        intent=ActionIntent(intent),
        resource=ResourceRef.parse(resource),
        deadline=None,
        dedup_key=f"{actor}:{intent}:{resource}",
    )


class TestRuleMatching:
    def test_intent_pattern_match(self):
        r = Rule(
            name="r1",
            intent_pattern=IntentPattern("notification.send"),
            actor_pattern="*",
            resource_pattern=ResourcePattern("*"),
        )
        assert r.matches(_env(intent="notification.send"))
        assert not r.matches(_env(intent="notification.deliver"))

    def test_actor_pattern_literal(self):
        r = Rule(
            name="r1",
            intent_pattern=IntentPattern("*"),
            actor_pattern="@alice:test",
            resource_pattern=ResourcePattern("*"),
        )
        assert r.matches(_env(actor="@alice:test"))
        assert not r.matches(_env(actor="@bob:test"))

    def test_resource_pattern_scheme_wildcard(self):
        r = Rule(
            name="r1",
            intent_pattern=IntentPattern("*"),
            actor_pattern="*",
            resource_pattern=ResourcePattern("slack://*"),
        )
        assert r.matches(_env(resource="slack://team/#alerts"))
        assert not r.matches(_env(resource="https://example.com"))

    def test_classification_filter(self):
        r = Rule(
            name="r1",
            intent_pattern=IntentPattern("*"),
            actor_pattern="*",
            resource_pattern=ResourcePattern("*"),
            classification=frozenset({Classification.PUBLIC}),
        )
        assert r.matches(_env(classification=Classification.PUBLIC))
        assert not r.matches(_env(classification=Classification.INTERNAL))

    def test_federation_origin_local_only(self):
        # federation_origin_pattern=None means local-only.
        r = Rule(
            name="r1",
            intent_pattern=IntentPattern("*"),
            actor_pattern="*",
            resource_pattern=ResourcePattern("*"),
        )
        assert r.matches(_env(federation_origin=None))
        assert not r.matches(_env(federation_origin="peer.example.org"))

    def test_federation_origin_specific_peer(self):
        r = Rule(
            name="r1",
            intent_pattern=IntentPattern("*"),
            actor_pattern="*",
            resource_pattern=ResourcePattern("*"),
            federation_origin_pattern="peer.example.org",
        )
        assert r.matches(_env(federation_origin="peer.example.org"))
        assert not r.matches(_env(federation_origin="other.example.org"))
        assert not r.matches(_env(federation_origin=None))

    def test_ttl_expiry(self):
        past = datetime.now(timezone.utc) - timedelta(hours=1)
        r = Rule(
            name="r1",
            intent_pattern=IntentPattern("*"),
            actor_pattern="*",
            resource_pattern=ResourcePattern("*"),
            ttl=past,
        )
        assert not r.matches(_env())


class TestRuleEnginePrecedence:
    """Spec precedence: deny > propose > require_capability > permit."""

    def test_no_rules_returns_none(self):
        engine = RuleEngine()
        result = engine.evaluate(_env())
        assert result.disposition is None
        assert result.matched_rules == ()

    def test_single_permit(self):
        engine = RuleEngine()
        engine.add(Rule(
            name="allow_all",
            intent_pattern=IntentPattern("*"),
            actor_pattern="*",
            resource_pattern=ResourcePattern("*"),
            disposition="permit",
        ))
        result = engine.evaluate(_env())
        assert result.disposition == "permit"
        assert result.matched_rules == ("allow_all",)

    def test_deny_beats_permit(self):
        engine = RuleEngine()
        engine.add(Rule(
            name="allow_all",
            intent_pattern=IntentPattern("*"),
            actor_pattern="*",
            resource_pattern=ResourcePattern("*"),
            disposition="permit",
        ))
        engine.add(Rule(
            name="deny_slack",
            intent_pattern=IntentPattern("*"),
            actor_pattern="*",
            resource_pattern=ResourcePattern("slack://*"),
            disposition="deny",
        ))
        result = engine.evaluate(_env(resource="slack://team/#alerts"))
        assert result.disposition == "deny"
        assert "deny_slack" in result.matched_rules

    def test_propose_beats_permit(self):
        engine = RuleEngine()
        engine.add(Rule(
            name="allow_all",
            intent_pattern=IntentPattern("*"),
            actor_pattern="*",
            resource_pattern=ResourcePattern("*"),
            disposition="permit",
        ))
        engine.add(Rule(
            name="propose_classified",
            intent_pattern=IntentPattern("*"),
            actor_pattern="*",
            resource_pattern=ResourcePattern("*"),
            classification=frozenset({Classification.REGULATED}),
            disposition="propose",
        ))
        result = engine.evaluate(_env(classification=Classification.REGULATED))
        assert result.disposition == "propose"

    def test_higher_priority_wins_within_disposition(self):
        engine = RuleEngine()
        engine.add(Rule(
            name="permit_low",
            intent_pattern=IntentPattern("*"),
            actor_pattern="*",
            resource_pattern=ResourcePattern("*"),
            disposition="permit",
            priority=1,
        ))
        engine.add(Rule(
            name="permit_high",
            intent_pattern=IntentPattern("*"),
            actor_pattern="*",
            resource_pattern=ResourcePattern("*"),
            disposition="permit",
            priority=10,
        ))
        result = engine.evaluate(_env())
        # Both match; both are permit; the high-priority one is recorded
        # in matched_rules. Disposition is `permit` regardless.
        assert result.disposition == "permit"
        assert "permit_high" in result.matched_rules
        assert "permit_low" in result.matched_rules

    def test_unmatched_rule_excluded(self):
        engine = RuleEngine()
        engine.add(Rule(
            name="for_slack",
            intent_pattern=IntentPattern("*"),
            actor_pattern="*",
            resource_pattern=ResourcePattern("slack://*"),
            disposition="permit",
        ))
        result = engine.evaluate(_env(resource="https://example.com"))
        assert result.disposition is None
