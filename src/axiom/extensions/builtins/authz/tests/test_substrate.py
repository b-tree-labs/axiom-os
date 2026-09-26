# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tests for the ADR-083 authorization-substrate seam in decide().

The substrate is consulted under the deterministic capability floor and above
the rule engine, with deny-overrides. The default NullSubstrate abstains, so the
pipeline stays behaviour-preserving until a real backend (OpenFGA, P2) registers.
"""

from __future__ import annotations

from axiom.extensions.builtins.authz.decide import DecideContext, decide
from axiom.extensions.builtins.authz.substrate import (
    AuthzSubstrate,
    DenyAllSubstrate,
    NullSubstrate,
    SubstrateDecision,
)
from axiom.governance import (
    ActionEnvelope,
    ActionIntent,
    CapabilityToken,
    Classification,
    Decision,
    NextAction,
    ProvenanceRef,
    ResourceRef,
    SubjectContext,
)
from axiom.vega.identity.principal import Principal


def _env(subject: SubjectContext | None = None) -> ActionEnvelope:
    alice = Principal(handle="@alice:test", public_bytes=b"\x00" * 32)
    return ActionEnvelope(
        actor=alice,
        capability=CapabilityToken.unscoped_test_token(subject=alice),
        classification=Classification.INTERNAL,
        context_fragment_id="memory://localhost/test",
        provenance_parent=ProvenanceRef.synthetic("test"),
        federation_origin=None,
        intent=ActionIntent("notification.send"),
        resource=ResourceRef.parse("slack://team-rsc/#alerts"),
        deadline=None,
        dedup_key="test-dedup",
        subject=subject,
    )


class _AllowSubstrate:
    def check(self, envelope: ActionEnvelope) -> SubstrateDecision:
        return SubstrateDecision.ALLOW


class _DenySubstrate:
    def check(self, envelope: ActionEnvelope) -> SubstrateDecision:
        return SubstrateDecision.DENY


# --- default / behaviour-preservation -------------------------------------


def test_default_substrate_is_null():
    assert isinstance(DecideContext().substrate, NullSubstrate)


def test_null_substrate_abstains_pipeline_unchanged():
    # NullSubstrate abstains → novel action still PROPOSE_TO_HUMAN, as before.
    verdict = decide(_env(), DecideContext())
    assert verdict.decision is Decision.PROPOSE_TO_HUMAN
    assert verdict.next_action_for_caller is NextAction.ENQUEUE_PROPOSAL


# --- deny-overrides --------------------------------------------------------


def test_substrate_deny_overrides():
    verdict = decide(_env(), DecideContext(substrate=_DenySubstrate()))
    assert verdict.decision is Decision.DENY
    assert verdict.next_action_for_caller is NextAction.ABORT
    assert "substrate" in verdict.reason
    assert verdict.receipt_fragment_id  # every path still writes a receipt


def test_deny_all_substrate_denies():
    verdict = decide(_env(), DecideContext(substrate=DenyAllSubstrate()))
    assert verdict.decision is Decision.DENY


def test_substrate_allow_is_authoritative_in_p2():
    # ADR-083 / P2: a substrate ALLOW is authoritative — it short-circuits the
    # novel-action propose default via the PolicySourceRegistry combiner.
    verdict = decide(_env(), DecideContext(substrate=_AllowSubstrate()))
    assert verdict.decision is Decision.PERMIT
    assert verdict.next_action_for_caller is NextAction.PROCEED


def test_curated_rule_still_outranks_substrate_allow():
    # A substrate ALLOW does not override an explicit "propose" rule — curated
    # policy wins over a relationship grant (rules priority > substrate).
    from axiom.extensions.builtins.authz.rules import Rule
    from axiom.governance import IntentPattern, ResourcePattern

    ctx = DecideContext(substrate=_AllowSubstrate())
    ctx.add_rule(
        Rule(
            name="review_notifications",
            intent_pattern=IntentPattern("notification.send"),
            actor_pattern="*",
            resource_pattern=ResourcePattern("slack://*"),
            disposition="propose",
        )
    )
    verdict = decide(_env(), ctx)
    assert verdict.decision is Decision.PROPOSE_TO_HUMAN


def test_substrate_deny_still_overrides_a_permit_rule():
    # Deny-overrides holds across sources: a substrate DENY beats a permit rule.
    from axiom.extensions.builtins.authz.rules import Rule
    from axiom.governance import IntentPattern, ResourcePattern

    ctx = DecideContext(substrate=_DenySubstrate())
    ctx.add_rule(
        Rule(
            name="allow_all_slack",
            intent_pattern=IntentPattern("*"),
            actor_pattern="*",
            resource_pattern=ResourcePattern("slack://*"),
            disposition="permit",
        )
    )
    verdict = decide(_env(), ctx)
    assert verdict.decision is Decision.DENY


# --- protocol + subject context -------------------------------------------


def test_substrates_satisfy_the_protocol():
    assert isinstance(NullSubstrate(), AuthzSubstrate)
    assert isinstance(DenyAllSubstrate(), AuthzSubstrate)
    assert isinstance(_DenySubstrate(), AuthzSubstrate)


def test_subject_context_carried_and_serialised():
    sc = SubjectContext(
        tenant="acme",
        fga_user="user:@alice:test",
        attributes={"mfa": True},
        contextual_tuples=(("user:@alice:test", "member", "team:rsc"),),
    )
    env = _env(subject=sc)
    assert env.subject is sc
    assert env.to_dict()["subject"] == {"tenant": "acme", "fga_user": "user:@alice:test"}


def test_subject_defaults_to_none():
    env = _env()
    assert env.subject is None
    assert "subject" not in env.to_dict()
