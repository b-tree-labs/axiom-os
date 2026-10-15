# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tests for classroom join-request signing + verification.

Tier A PR 2 — locks the request wire format and the signing/verification
contract that the A2A handshake (PR 3) will ride on top of.

A ClassroomJoinRequest binds four things into a single signed payload:
  - the invite's secret token (proof the student was invited)
  - the classroom_id (scoped to this cohort)
  - the student's member_node (their public-key-derived node_id)
  - the student's public_key (so the coordinator can verify the sig)

The signature covers a canonical JSON encoding of the above. Verification
reconstructs the canonical form and uses the embedded public_key to
check the Ed25519 signature.
"""

from __future__ import annotations

import json

import pytest

from axiom.extensions.builtins.classroom.classroom_join_request import (
    ClassroomJoinRequest,
    InvalidJoinRequestError,
    canonical_signing_payload,
    decode_join_request,
    encode_join_request,
    sign_join_request,
    verify_join_request,
)
from axiom.extensions.builtins.classroom.invite_token import create_invite_token
from axiom.vega.federation.identity import generate_identity

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def student_identity(tmp_path):
    return generate_identity(
        owner="alice@example.org",
        display_name="Alice's Laptop",
        keys_dir=tmp_path / "student-keys",
    )


@pytest.fixture
def other_identity(tmp_path):
    return generate_identity(
        owner="bob@example.org",
        display_name="Bob's Laptop",
        keys_dir=tmp_path / "other-keys",
    )


@pytest.fixture
def invite():
    return create_invite_token(
        classroom_id="ne101-prague-2026",
        coordinator_id="peer_node_abc",
        ttl_hours=24,
    )


# ---------------------------------------------------------------------------
# Shape + canonical payload
# ---------------------------------------------------------------------------


class TestJoinRequestShape:
    def test_sign_produces_all_required_fields(self, student_identity, invite):
        req = sign_join_request(
            identity=student_identity,
            invite=invite,
            student_id="alice",
        )
        assert req.student_id == "alice"
        assert req.member_node == student_identity.node_id
        assert req.public_key == student_identity.public_key
        assert req.invite_token == invite.token
        assert req.classroom_id == invite.classroom_id
        assert req.signature  # non-empty

    def test_canonical_payload_is_deterministic(self, student_identity, invite):
        """Two requests with the same inputs must produce the same payload bytes."""
        req1 = sign_join_request(student_identity, invite, "alice")
        req2 = ClassroomJoinRequest(
            student_id=req1.student_id,
            member_node=req1.member_node,
            public_key=req1.public_key,
            invite_token=req1.invite_token,
            classroom_id=req1.classroom_id,
            signature="",  # irrelevant for canonical payload
        )
        assert canonical_signing_payload(req1) == canonical_signing_payload(req2)

    def test_canonical_payload_excludes_signature(self, student_identity, invite):
        """Signature field must NOT be included in the signed-over bytes."""
        req = sign_join_request(student_identity, invite, "alice")
        payload = canonical_signing_payload(req)
        assert req.signature not in payload.decode("utf-8", errors="ignore")


# ---------------------------------------------------------------------------
# Signing + verification
# ---------------------------------------------------------------------------


class TestSignatureVerification:
    def test_fresh_request_verifies(self, student_identity, invite):
        req = sign_join_request(student_identity, invite, "alice")
        result = verify_join_request(req, expected_classroom_id=invite.classroom_id)
        assert result.valid is True
        assert result.reason is None

    def test_tampered_student_id_fails_verification(self, student_identity, invite):
        req = sign_join_request(student_identity, invite, "alice")
        # Construct a tampered request with a new student_id but original signature.
        tampered = ClassroomJoinRequest(
            student_id="mallory",  # changed
            member_node=req.member_node,
            public_key=req.public_key,
            invite_token=req.invite_token,
            classroom_id=req.classroom_id,
            signature=req.signature,
        )
        result = verify_join_request(tampered, expected_classroom_id=invite.classroom_id)
        assert result.valid is False
        assert "signature" in result.reason.lower()

    def test_tampered_classroom_id_fails_verification(self, student_identity, invite):
        req = sign_join_request(student_identity, invite, "alice")
        tampered = ClassroomJoinRequest(
            student_id=req.student_id,
            member_node=req.member_node,
            public_key=req.public_key,
            invite_token=req.invite_token,
            classroom_id="different-classroom",
            signature=req.signature,
        )
        result = verify_join_request(tampered, expected_classroom_id="different-classroom")
        assert result.valid is False
        assert "signature" in result.reason.lower()

    def test_wrong_public_key_fails_verification(self, student_identity, other_identity, invite):
        req = sign_join_request(student_identity, invite, "alice")
        # Replace only the public_key with someone else's — signature will no longer verify.
        swapped = ClassroomJoinRequest(
            student_id=req.student_id,
            member_node=req.member_node,
            public_key=other_identity.public_key,
            invite_token=req.invite_token,
            classroom_id=req.classroom_id,
            signature=req.signature,
        )
        result = verify_join_request(swapped, expected_classroom_id=invite.classroom_id)
        assert result.valid is False

    def test_classroom_id_mismatch_with_expected_fails(self, student_identity, invite):
        """Coordinator must assert the request is for its own classroom."""
        req = sign_join_request(student_identity, invite, "alice")
        result = verify_join_request(req, expected_classroom_id="wrong-classroom")
        assert result.valid is False
        assert "classroom" in result.reason.lower()

    def test_garbage_signature_fails_verification(self, student_identity, invite):
        req = sign_join_request(student_identity, invite, "alice")
        tampered = ClassroomJoinRequest(
            student_id=req.student_id,
            member_node=req.member_node,
            public_key=req.public_key,
            invite_token=req.invite_token,
            classroom_id=req.classroom_id,
            signature="not-a-valid-signature",
        )
        result = verify_join_request(tampered, expected_classroom_id=invite.classroom_id)
        assert result.valid is False


# ---------------------------------------------------------------------------
# Wire format
# ---------------------------------------------------------------------------


class TestWireFormat:
    def test_encode_decode_roundtrip(self, student_identity, invite):
        req = sign_join_request(student_identity, invite, "alice")
        encoded = encode_join_request(req)
        decoded = decode_join_request(encoded)
        assert decoded == req

    def test_encoded_is_single_line(self, student_identity, invite):
        req = sign_join_request(student_identity, invite, "alice")
        encoded = encode_join_request(req)
        assert "\n" not in encoded

    def test_decode_garbage_raises(self):
        with pytest.raises(InvalidJoinRequestError):
            decode_join_request("not-a-valid-request")

    def test_decode_empty_raises(self):
        with pytest.raises(InvalidJoinRequestError):
            decode_join_request("")

    def test_decode_missing_fields_raises(self):
        import base64
        partial = {"student_id": "alice"}  # missing everything else
        malformed = base64.urlsafe_b64encode(
            json.dumps(partial).encode()
        ).decode().rstrip("=")
        with pytest.raises(InvalidJoinRequestError):
            decode_join_request(malformed)

    def test_roundtrip_preserves_verifiability(self, student_identity, invite):
        """Serialize → deserialize → verify — critical for A2A round-trip."""
        original = sign_join_request(student_identity, invite, "alice")
        encoded = encode_join_request(original)
        decoded = decode_join_request(encoded)
        result = verify_join_request(decoded, expected_classroom_id=invite.classroom_id)
        assert result.valid is True
