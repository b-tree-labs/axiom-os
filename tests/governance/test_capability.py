# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tests for `axiom.governance.capability` — capability tokens.

Per spec-governance-fabric §2: tokens are cryptographically bound to their
issuer, scoped to verb + resource patterns, classification-ceiling-enforced,
and time-bounded. These tests cover the type contract; KEEP's vault module
covers issuance, signing, revocation lifecycle.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from axiom.governance.capability import CapabilityToken
from axiom.governance.classification import Classification
from axiom.governance.intent import ActionIntent, IntentPattern
from axiom.governance.resource import ResourcePattern, ResourceRef
from axiom.vega.identity.principal import Principal


def _alice() -> Principal:
    return Principal(handle="@alice:test", public_bytes=b"\x00" * 32)


def _issuer() -> Principal:
    return Principal(handle="@vault:localhost", public_bytes=b"\x01" * 32)


def _token(**overrides) -> CapabilityToken:
    now = datetime.now(timezone.utc)
    defaults = dict(
        id="01HZTEST0000000000000",
        issuer=_issuer(),
        subject=_alice(),
        intent_pattern=IntentPattern("notification.*"),
        resource_pattern=ResourcePattern("slack://*"),
        classification_ceiling=Classification.INTERNAL,
        not_before=now - timedelta(seconds=1),
        not_after=now + timedelta(hours=1),
        delegation_depth=0,
        parent_capability=None,
        signature=b"\xff" * 64,  # placeholder; KEEP validates real signatures
    )
    defaults.update(overrides)
    return CapabilityToken(**defaults)


class TestTokenConstruction:
    def test_basic_token(self):
        tok = _token()
        assert tok.subject.handle == "@alice:test"
        assert tok.intent_pattern.value == "notification.*"

    def test_token_is_frozen(self):
        from dataclasses import FrozenInstanceError

        tok = _token()
        with pytest.raises(FrozenInstanceError):
            tok.subject = _issuer()


class TestTokenScopeChecks:
    def test_intent_in_scope(self):
        tok = _token()
        assert tok.permits_intent(ActionIntent("notification.send"))
        assert tok.permits_intent(ActionIntent("notification.deliver"))

    def test_intent_out_of_scope(self):
        tok = _token()
        assert not tok.permits_intent(ActionIntent("vault.issue_capability"))

    def test_resource_in_scope(self):
        tok = _token()
        assert tok.permits_resource(ResourceRef.parse("slack://team-rsc/#alerts"))

    def test_resource_out_of_scope(self):
        tok = _token()
        assert not tok.permits_resource(ResourceRef.parse("https://example.com"))

    def test_classification_at_ceiling(self):
        tok = _token()
        assert tok.permits_classification(Classification.INTERNAL)
        assert tok.permits_classification(Classification.PUBLIC)

    def test_classification_above_ceiling_denied(self):
        tok = _token(classification_ceiling=Classification.INTERNAL)
        assert not tok.permits_classification(Classification.REGULATED)
        assert not tok.permits_classification(Classification.CONTROLLED)


class TestTokenLifecycle:
    def test_valid_now_when_in_window(self):
        tok = _token()
        assert tok.is_valid_at(datetime.now(timezone.utc))

    def test_invalid_before_not_before(self):
        now = datetime.now(timezone.utc)
        tok = _token(not_before=now + timedelta(minutes=5))
        assert not tok.is_valid_at(now)

    def test_invalid_after_not_after(self):
        now = datetime.now(timezone.utc)
        tok = _token(not_after=now - timedelta(seconds=1))
        assert not tok.is_valid_at(now)


class TestTokenDelegation:
    def test_leaf_token_cannot_delegate(self):
        tok = _token(delegation_depth=0)
        assert not tok.can_delegate

    def test_parent_token_can_delegate(self):
        tok = _token(delegation_depth=2)
        assert tok.can_delegate
        # Each delegation reduces depth.
        child_depth = tok.delegation_depth - 1
        assert child_depth == 1


class TestUnscopedTestToken:
    """Tests for the test helper used in envelope tests."""

    def test_helper_constructs_valid_token(self):
        alice = _alice()
        tok = CapabilityToken.unscoped_test_token(subject=alice)
        assert tok.subject is alice
        assert tok.is_valid_at(datetime.now(timezone.utc))
        assert tok.intent_pattern.matches(ActionIntent("notification.send"))
