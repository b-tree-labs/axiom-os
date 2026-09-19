# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tests for `axiom.governance.envelope` — the action envelope.

Per spec-governance-fabric §1: every action that crosses a trust,
classification, or ownership boundary on the platform carries an
`ActionEnvelope`. The envelope is the universal currency every primitive
consumes.
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from datetime import datetime, timedelta, timezone

import pytest

from axiom.governance.capability import CapabilityToken
from axiom.governance.classification import Classification
from axiom.governance.envelope import ActionEnvelope
from axiom.governance.intent import ActionIntent
from axiom.governance.provenance import ProvenanceRef
from axiom.governance.resource import ResourceRef
from axiom.vega.identity.principal import Principal


def _alice() -> Principal:
    return Principal(handle="@alice:test", public_bytes=b"\x00" * 32)


def _stub_capability() -> CapabilityToken:
    return CapabilityToken.unscoped_test_token(subject=_alice())


def _envelope(**overrides) -> ActionEnvelope:
    defaults = dict(
        actor=_alice(),
        capability=_stub_capability(),
        classification=Classification.INTERNAL,
        context_fragment_id="memory://localhost/contexts/test",
        provenance_parent=ProvenanceRef.synthetic("test"),
        federation_origin=None,
        intent=ActionIntent("notification.send"),
        resource=ResourceRef.channel("slack://team-rsc/#alerts"),
        deadline=None,
        dedup_key="test-dedup",
    )
    defaults.update(overrides)
    return ActionEnvelope(**defaults)


class TestEnvelopeConstruction:
    def test_basic_envelope_constructs(self):
        env = _envelope()
        assert env.actor.handle == "@alice:test"
        assert env.intent.value == "notification.send"
        assert env.classification is Classification.INTERNAL

    def test_envelope_is_frozen(self):
        env = _envelope()
        with pytest.raises(FrozenInstanceError):
            env.actor = _alice()

    def test_local_envelope_has_no_federation_origin(self):
        env = _envelope()
        assert env.federation_origin is None
        assert env.is_local

    def test_federation_envelope_has_origin(self):
        env = _envelope(federation_origin="peer.example.org")
        assert env.federation_origin == "peer.example.org"
        assert not env.is_local

    def test_deadline_optional(self):
        deadline = datetime.now(timezone.utc) + timedelta(hours=1)
        env = _envelope(deadline=deadline)
        assert env.deadline == deadline


class TestEnvelopeValidation:
    def test_unregistered_intent_rejected_when_strict(self):
        # The lint catches this; constructing with `strict=True` mimics
        # the lint's runtime assertion.
        with pytest.raises(ValueError, match="unregistered intent"):
            _envelope(intent=ActionIntent("nuclear.do_thing"), strict=True)

    def test_unregistered_intent_allowed_in_dev_mode(self):
        # In dev / test mode (default), the envelope construction is
        # permissive; the static-analysis lint is the binding gate.
        env = _envelope(intent=ActionIntent("nuclear.do_thing"))
        assert env.intent.value == "nuclear.do_thing"

    def test_empty_dedup_key_rejected(self):
        with pytest.raises(ValueError, match="dedup_key"):
            _envelope(dedup_key="")


class TestEnvelopeDedupKey:
    """The dedup_key is the load-bearing idempotency identifier (spec §6.1)."""

    def test_same_inputs_same_dedup_key(self):
        env1 = _envelope(dedup_key="x")
        env2 = _envelope(dedup_key="x")
        assert env1.dedup_key == env2.dedup_key

    def test_different_dedup_keys(self):
        env1 = _envelope(dedup_key="a")
        env2 = _envelope(dedup_key="b")
        assert env1.dedup_key != env2.dedup_key


class TestEnvelopeSerialization:
    """Envelopes serialize for receipt fragments (spec §4.1)."""

    def test_to_dict_round_trip(self):
        env = _envelope()
        d = env.to_dict()
        assert d["actor"] == "@alice:test"
        assert d["intent"] == "notification.send"
        assert d["classification"] == "internal"
        assert d["resource"] == "slack://team-rsc/#alerts"
        assert d["dedup_key"] == "test-dedup"

    def test_to_dict_federation_origin_when_set(self):
        env = _envelope(federation_origin="peer.example.org")
        d = env.to_dict()
        assert d["federation_origin"] == "peer.example.org"

    def test_to_dict_omits_none_federation_origin(self):
        env = _envelope()
        d = env.to_dict()
        assert d.get("federation_origin") is None
