# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""OpenFgaSubstrate — GUARD's relationship-based authorization backend (ADR-083).

The live OpenFGA server (and the recall/latency benchmark gate) is deferred — it
needs OpenFGA-on-Postgres, which the sandbox lacks. These tests exercise the
substrate's *logic* against a fake check-client: the allow/deny/abstain mapping,
the envelope→tuple derivation, contextual tuples, and error handling.
"""

from __future__ import annotations

from axiom.extensions.builtins.authz.openfga import (
    FgaCheckSpec,
    OpenFgaSubstrate,
    default_mapper,
)
from axiom.extensions.builtins.authz.substrate import AuthzSubstrate, SubstrateDecision
from axiom.governance import (
    ActionEnvelope,
    ActionIntent,
    CapabilityToken,
    Classification,
    ProvenanceRef,
    ResourceRef,
    SubjectContext,
)
from axiom.vega.identity.principal import Principal


def _env(subject: SubjectContext | None = None, intent="notification.send",
         resource="slack://team-rsc/#alerts") -> ActionEnvelope:
    alice = Principal(handle="@alice:test", public_bytes=b"\x00" * 32)
    return ActionEnvelope(
        actor=alice,
        capability=CapabilityToken.unscoped_test_token(subject=alice),
        classification=Classification.INTERNAL,
        context_fragment_id="memory://localhost/test",
        provenance_parent=ProvenanceRef.synthetic("test"),
        federation_origin=None,
        intent=ActionIntent(intent),
        resource=ResourceRef.parse(resource),
        deadline=None,
        dedup_key="test-dedup",
        subject=subject,
    )


class _FakeFga:
    """Records checks; returns True for seeded (user, relation, object) triples."""

    def __init__(self, tuples=(), *, raises=False):
        self.tuples = set(tuples)
        self.calls: list[tuple] = []
        self.raises = raises

    def check(self, *, user, relation, object, contextual_tuples=()):
        if self.raises:
            raise RuntimeError("openfga unreachable")
        self.calls.append((user, relation, object, tuple(contextual_tuples)))
        if (user, relation, object) in self.tuples:
            return True
        return (user, relation, object) in set(contextual_tuples)


OBJ = "slack:team-rsc/#alerts"
USER = "user:@alice:test"
REL = "notification_send"


# --- allow / deny / abstain ------------------------------------------------


def test_permit_relation_grants_allow():
    fga = _FakeFga({(USER, REL, OBJ)})
    assert OpenFgaSubstrate(fga).check(_env()) is SubstrateDecision.ALLOW


def test_no_relation_abstains():
    fga = _FakeFga(set())
    assert OpenFgaSubstrate(fga).check(_env()) is SubstrateDecision.ABSTAIN


def test_blocked_relation_denies():
    fga = _FakeFga({(USER, "blocked", OBJ)})
    assert OpenFgaSubstrate(fga).check(_env()) is SubstrateDecision.DENY


def test_blocked_overrides_permit():
    # Even with the permit relation present, a blocked tuple denies.
    fga = _FakeFga({(USER, REL, OBJ), (USER, "blocked", OBJ)})
    assert OpenFgaSubstrate(fga).check(_env()) is SubstrateDecision.DENY


# --- envelope -> tuple mapping ---------------------------------------------


def test_default_mapping_derives_user_object_relation():
    spec = default_mapper(_env())
    assert spec == FgaCheckSpec(
        user=USER, object=OBJ, permit_relation=REL, deny_relation="blocked",
        contextual_tuples=(),
    )


def test_subject_fga_user_preferred_over_actor_handle():
    sc = SubjectContext(fga_user="user:alice@corp")
    spec = default_mapper(_env(subject=sc))
    assert spec.user == "user:alice@corp"


def test_contextual_tuples_passed_through():
    ct = (("user:@alice:test", "member", "group:oncall"),)
    sc = SubjectContext(fga_user=USER, contextual_tuples=ct)
    fga = _FakeFga(set())
    OpenFgaSubstrate(fga).check(_env(subject=sc))
    # Both the blocked and permit checks receive the contextual tuples.
    assert fga.calls[0][3] == ct


def test_intent_becomes_underscored_relation():
    spec = default_mapper(_env(intent="memory.write.sensitive"))
    assert spec.permit_relation == "memory_write_sensitive"


# --- error handling --------------------------------------------------------


def test_error_defaults_to_abstain():
    fga = _FakeFga(raises=True)
    assert OpenFgaSubstrate(fga).check(_env()) is SubstrateDecision.ABSTAIN


def test_error_can_fail_closed_when_configured():
    fga = _FakeFga(raises=True)
    substrate = OpenFgaSubstrate(fga, on_error=SubstrateDecision.DENY)
    assert substrate.check(_env()) is SubstrateDecision.DENY


def test_mapper_returning_none_abstains():
    fga = _FakeFga({(USER, REL, OBJ)})
    substrate = OpenFgaSubstrate(fga, mapper=lambda env: None)
    assert substrate.check(_env()) is SubstrateDecision.ABSTAIN
    assert fga.calls == []  # never consulted


# --- protocol conformance --------------------------------------------------


def test_satisfies_authz_substrate_protocol():
    assert isinstance(OpenFgaSubstrate(_FakeFga()), AuthzSubstrate)
