# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tests for `axiom.governance.intent` — registered action ontology.

Per spec-governance-fabric §1.3: actions name themselves with verbs from a
**registered ontology**, not free-form strings. The lint refuses an
`ActionEnvelope` with an unregistered intent.
"""

from __future__ import annotations

import pytest

from axiom.governance.intent import (
    ActionIntent,
    IntentPattern,
    REGISTERED_INTENTS,
    register_intent,
)


class TestActionIntent:
    def test_parses_dotted_form(self):
        intent = ActionIntent("notification.send")
        assert intent.primitive == "notification"
        assert intent.verb == "send"

    def test_parses_three_part(self):
        intent = ActionIntent("federation.share_fragment.cohort_internal")
        assert intent.primitive == "federation"
        assert intent.verb == "share_fragment"
        assert intent.qualifier == "cohort_internal"

    def test_empty_raises(self):
        with pytest.raises(ValueError):
            ActionIntent("")

    def test_no_dot_raises(self):
        with pytest.raises(ValueError):
            ActionIntent("send")

    def test_string_round_trip(self):
        intent = ActionIntent("vault.issue_capability")
        assert str(intent) == "vault.issue_capability"


class TestRegisteredOntology:
    def test_registered_intents_seeded_per_spec(self):
        # Sanity: every primitive named in spec §1.3 is present.
        for name in [
            "authz.permit",
            "authz.deny",
            "authz.propose",
            "vault.issue_capability",
            "vault.rotate_secret",
            "vault.revoke_capability",
            "vault.read_secret",
            "notification.send",
            "notification.deliver",
            "notification.receive",
            "schedule.fire",
            "schedule.skip",
            "schedule.retry",
            "schedule.dead_letter",
        ]:
            assert name in REGISTERED_INTENTS, f"{name} missing from ontology"

    def test_unregistered_intent_rejected(self):
        # Constructing an envelope with an unregistered intent must be
        # detectable; the lint hooks on this exact check.
        intent = ActionIntent("nuclear.do_thing")
        assert not intent.is_registered()

    def test_registered_intent_accepted(self):
        intent = ActionIntent("notification.send")
        assert intent.is_registered()

    def test_register_intent_extends_ontology(self):
        # Extensions can register their own verbs (spec §1.3 amendment).
        try:
            register_intent("experiment.transition")
            assert ActionIntent("experiment.transition").is_registered()
        finally:
            # Clean up so this test is order-independent.
            REGISTERED_INTENTS.discard("experiment.transition")


class TestIntentPattern:
    def test_exact_match(self):
        pat = IntentPattern("notification.send")
        assert pat.matches(ActionIntent("notification.send"))
        assert not pat.matches(ActionIntent("notification.deliver"))

    def test_wildcard_within_primitive(self):
        pat = IntentPattern("notification.*")
        assert pat.matches(ActionIntent("notification.send"))
        assert pat.matches(ActionIntent("notification.deliver"))
        assert pat.matches(ActionIntent("notification.receive"))
        assert not pat.matches(ActionIntent("vault.issue_capability"))

    def test_universal_wildcard(self):
        pat = IntentPattern("*")
        assert pat.matches(ActionIntent("anything.at_all"))

    def test_primitive_only_pattern(self):
        # `notification` (no dot, no wildcard) is treated as primitive-scope.
        pat = IntentPattern("notification")
        assert pat.matches(ActionIntent("notification.send"))
        assert not pat.matches(ActionIntent("vault.issue_capability"))
