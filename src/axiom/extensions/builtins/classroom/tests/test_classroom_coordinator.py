# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tests for the coordinator-side classroom-join handler.

Tier A PR 3 (logic-only) — composes invite-token validation (PR 1) +
join-request verification (PR 2) into the full ceremony: student signs
→ coordinator validates → cohort updated → coordinator signs +
returns a membership manifest.

This PR intentionally stops short of HTTP: ``process_join_request`` is
a pure function. A thin A2A wrapper can wire this into an HTTP endpoint
in a subsequent PR without changing any logic here.
"""

from __future__ import annotations

import pytest

from axiom.extensions.builtins.classroom.classroom_coordinator import (
    InMemoryInviteRegistry,
    MembershipManifest,
    decode_membership_manifest,
    encode_membership_manifest,
    process_join_request,
    sign_membership_manifest,
    verify_membership_manifest,
)
from axiom.extensions.builtins.classroom.classroom_federation import create_cohort
from axiom.extensions.builtins.classroom.classroom_join_request import (
    encode_join_request,
    sign_join_request,
)
from axiom.extensions.builtins.classroom.invite_token import create_invite_token
from axiom.vega.federation.identity import generate_identity

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def coordinator_identity(tmp_path):
    return generate_identity(
        owner="ondrej@ctu.cz",
        display_name="Test Coordinator",
        keys_dir=tmp_path / "coordinator-keys",
    )


@pytest.fixture
def student_identity(tmp_path):
    return generate_identity(
        owner="alice@example.org",
        display_name="Alice's Laptop",
        keys_dir=tmp_path / "alice-keys",
    )


@pytest.fixture
def other_coordinator_identity(tmp_path):
    return generate_identity(
        owner="mallory@nowhere",
        display_name="Mallory's Fake Coordinator",
        keys_dir=tmp_path / "fake-coord-keys",
    )


@pytest.fixture
def cohort(coordinator_identity):
    return create_cohort(
        classroom_id="ne101-prague-2026",
        coordinator_node=coordinator_identity.node_id,
    )


@pytest.fixture
def invite_registry():
    return InMemoryInviteRegistry()


def _issue_and_encode_request(
    *,
    cohort,
    student_identity,
    invite_registry,
    student_id="alice",
    ttl_hours=24,
):
    """Instructor mints invite + student signs request. Returns (encoded_request, invite)."""
    invite = create_invite_token(
        classroom_id=cohort.classroom_id,
        coordinator_id=cohort.coordinator_node,
        ttl_hours=ttl_hours,
    )
    invite_registry.register(invite)
    request = sign_join_request(
        identity=student_identity,
        invite=invite,
        student_id=student_id,
    )
    return encode_join_request(request), invite


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


class TestHappyPath:
    def test_valid_request_produces_signed_manifest(
        self, coordinator_identity, student_identity, cohort, invite_registry
    ):
        encoded, _ = _issue_and_encode_request(
            cohort=cohort,
            student_identity=student_identity,
            invite_registry=invite_registry,
        )
        result = process_join_request(
            encoded_request=encoded,
            coordinator_identity=coordinator_identity,
            cohort=cohort,
            invite_registry=invite_registry,
        )
        assert result.accepted is True
        assert result.error is None
        assert result.manifest is not None
        assert result.manifest.classroom_id == cohort.classroom_id
        assert result.manifest.student_id == "alice"
        assert result.manifest.coordinator_node == coordinator_identity.node_id
        assert result.manifest.joined_at
        assert result.manifest.signature

    def test_cohort_gains_new_member_after_successful_join(
        self, coordinator_identity, student_identity, cohort, invite_registry
    ):
        encoded, _ = _issue_and_encode_request(
            cohort=cohort,
            student_identity=student_identity,
            invite_registry=invite_registry,
        )
        assert len(cohort.members) == 0

        result = process_join_request(
            encoded_request=encoded,
            coordinator_identity=coordinator_identity,
            cohort=cohort,
            invite_registry=invite_registry,
        )
        assert result.accepted
        assert result.updated_cohort is not None
        assert len(result.updated_cohort.members) == 1
        assert result.updated_cohort.members[0].student_id == "alice"
        assert result.updated_cohort.members[0].member_node == student_identity.node_id
        assert result.updated_cohort.members[0].status == "ACTIVE"

    def test_manifest_verifies_against_coordinator_pubkey(
        self, coordinator_identity, student_identity, cohort, invite_registry
    ):
        encoded, _ = _issue_and_encode_request(
            cohort=cohort,
            student_identity=student_identity,
            invite_registry=invite_registry,
        )
        result = process_join_request(
            encoded_request=encoded,
            coordinator_identity=coordinator_identity,
            cohort=cohort,
            invite_registry=invite_registry,
        )
        check = verify_membership_manifest(
            result.manifest,
            coordinator_public_key=coordinator_identity.public_key,
        )
        assert check.valid is True


# ---------------------------------------------------------------------------
# Rejection paths
# ---------------------------------------------------------------------------


class TestRejection:
    def test_unknown_invite_rejected(
        self, coordinator_identity, student_identity, cohort, invite_registry
    ):
        # Mint an invite but DON'T register it with the coordinator.
        invite = create_invite_token(
            classroom_id=cohort.classroom_id,
            coordinator_id=cohort.coordinator_node,
            ttl_hours=24,
        )
        request = sign_join_request(
            identity=student_identity, invite=invite, student_id="alice"
        )
        encoded = encode_join_request(request)
        result = process_join_request(
            encoded_request=encoded,
            coordinator_identity=coordinator_identity,
            cohort=cohort,
            invite_registry=invite_registry,
        )
        assert result.accepted is False
        assert result.manifest is None
        assert "invite" in result.error.lower()

    def test_expired_invite_rejected(
        self, coordinator_identity, student_identity, cohort, invite_registry
    ):
        # ttl=0 — expires immediately (before any verification step runs).
        encoded, _ = _issue_and_encode_request(
            cohort=cohort,
            student_identity=student_identity,
            invite_registry=invite_registry,
            ttl_hours=0,
        )
        result = process_join_request(
            encoded_request=encoded,
            coordinator_identity=coordinator_identity,
            cohort=cohort,
            invite_registry=invite_registry,
        )
        assert result.accepted is False
        assert "expired" in result.error.lower()

    def test_consumed_invite_rejected_on_reuse(
        self, coordinator_identity, student_identity, cohort, invite_registry
    ):
        encoded, invite = _issue_and_encode_request(
            cohort=cohort,
            student_identity=student_identity,
            invite_registry=invite_registry,
        )
        # First use — should succeed AND mark the invite consumed.
        first = process_join_request(
            encoded_request=encoded,
            coordinator_identity=coordinator_identity,
            cohort=cohort,
            invite_registry=invite_registry,
        )
        assert first.accepted is True
        # Second use of same invite must fail — token-reuse defense.
        second = process_join_request(
            encoded_request=encoded,
            coordinator_identity=coordinator_identity,
            cohort=first.updated_cohort,  # updated state
            invite_registry=invite_registry,
        )
        assert second.accepted is False
        assert "consumed" in second.error.lower() or "reuse" in second.error.lower()

    def test_tampered_request_rejected(
        self, coordinator_identity, student_identity, cohort, invite_registry
    ):
        encoded, _ = _issue_and_encode_request(
            cohort=cohort,
            student_identity=student_identity,
            invite_registry=invite_registry,
        )
        # Tamper the encoded payload by flipping some characters.
        # It'll still be valid base64 structurally but signature won't match.
        tampered = encoded[:-8] + ("X" * 8)
        result = process_join_request(
            encoded_request=tampered,
            coordinator_identity=coordinator_identity,
            cohort=cohort,
            invite_registry=invite_registry,
        )
        assert result.accepted is False

    def test_wrong_classroom_rejected(
        self, coordinator_identity, student_identity, cohort, invite_registry
    ):
        # Student signs a request claiming a different classroom_id than the
        # coordinator hosts. Coordinator rejects.
        foreign_invite = create_invite_token(
            classroom_id="some-other-classroom",
            coordinator_id=cohort.coordinator_node,
            ttl_hours=24,
        )
        invite_registry.register(foreign_invite)
        request = sign_join_request(
            identity=student_identity, invite=foreign_invite, student_id="alice"
        )
        encoded = encode_join_request(request)
        result = process_join_request(
            encoded_request=encoded,
            coordinator_identity=coordinator_identity,
            cohort=cohort,
            invite_registry=invite_registry,
        )
        assert result.accepted is False
        assert "classroom" in result.error.lower()

    def test_garbage_request_rejected(
        self, coordinator_identity, cohort, invite_registry
    ):
        result = process_join_request(
            encoded_request="not-a-valid-request",
            coordinator_identity=coordinator_identity,
            cohort=cohort,
            invite_registry=invite_registry,
        )
        assert result.accepted is False
        assert result.error


# ---------------------------------------------------------------------------
# Manifest signing / verification
# ---------------------------------------------------------------------------


class TestMembershipManifest:
    def test_sign_manifest_produces_required_fields(
        self, coordinator_identity, cohort
    ):
        from axiom.extensions.builtins.classroom.classroom_federation import add_member

        cohort = add_member(
            cohort,
            student_id="alice",
            member_node="alice_node_abc",
            invite_token="t",
        )
        manifest = sign_membership_manifest(
            identity=coordinator_identity,
            cohort=cohort,
            student_id="alice",
        )
        assert manifest.classroom_id == cohort.classroom_id
        assert manifest.student_id == "alice"
        assert manifest.member_node == "alice_node_abc"
        assert manifest.coordinator_node == coordinator_identity.node_id
        assert manifest.status == "ACTIVE"
        assert manifest.joined_at
        assert manifest.signature

    def test_verify_detects_tamper(
        self, coordinator_identity, cohort
    ):
        from axiom.extensions.builtins.classroom.classroom_federation import add_member

        cohort = add_member(cohort, "alice", "alice_node", "t")
        manifest = sign_membership_manifest(
            identity=coordinator_identity, cohort=cohort, student_id="alice"
        )
        tampered = MembershipManifest(
            classroom_id=manifest.classroom_id,
            student_id="mallory",  # changed
            member_node=manifest.member_node,
            coordinator_node=manifest.coordinator_node,
            status=manifest.status,
            joined_at=manifest.joined_at,
            signature=manifest.signature,
        )
        result = verify_membership_manifest(
            tampered, coordinator_public_key=coordinator_identity.public_key
        )
        assert result.valid is False

    def test_verify_rejects_wrong_coordinator_pubkey(
        self, coordinator_identity, other_coordinator_identity, cohort
    ):
        from axiom.extensions.builtins.classroom.classroom_federation import add_member

        cohort = add_member(cohort, "alice", "alice_node", "t")
        manifest = sign_membership_manifest(
            identity=coordinator_identity, cohort=cohort, student_id="alice"
        )
        # Verify with the WRONG coordinator's public key.
        result = verify_membership_manifest(
            manifest, coordinator_public_key=other_coordinator_identity.public_key
        )
        assert result.valid is False

    def test_manifest_wire_roundtrip(self, coordinator_identity, cohort):
        from axiom.extensions.builtins.classroom.classroom_federation import add_member

        cohort = add_member(cohort, "alice", "alice_node", "t")
        original = sign_membership_manifest(
            identity=coordinator_identity, cohort=cohort, student_id="alice"
        )
        encoded = encode_membership_manifest(original)
        decoded = decode_membership_manifest(encoded)
        assert decoded == original
        # Verification still passes after roundtrip.
        result = verify_membership_manifest(
            decoded, coordinator_public_key=coordinator_identity.public_key
        )
        assert result.valid is True


# ---------------------------------------------------------------------------
# InMemoryInviteRegistry
# ---------------------------------------------------------------------------


class TestInviteRegistry:
    def test_register_then_lookup(self, invite_registry):
        invite = create_invite_token("c1", "n1", 24)
        invite_registry.register(invite)
        looked_up = invite_registry.find_by_token(invite.token)
        assert looked_up is not None
        assert looked_up.classroom_id == invite.classroom_id

    def test_unknown_token_returns_none(self, invite_registry):
        assert invite_registry.find_by_token("no-such-token") is None

    def test_mark_consumed_then_is_consumed(self, invite_registry):
        invite = create_invite_token("c1", "n1", 24)
        invite_registry.register(invite)
        assert invite_registry.is_consumed(invite.token) is False
        invite_registry.mark_consumed(invite.token)
        assert invite_registry.is_consumed(invite.token) is True
