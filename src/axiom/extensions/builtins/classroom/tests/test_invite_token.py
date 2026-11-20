# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tests for classroom invite-token format + validation.

First TDD step on the Tier A classroom-federation-ceremony work
(per ``docs/working/tier-a-federation-ceremony-roadmap.md``). Locks
the on-wire invite format before any handshake code lands:

    {"token": <base64>, "classroom_id": <uuid>,
     "coordinator_id": <node_id>, "expires": <iso8601>}

Encoded as base64(json(payload)) so the full invite fits in a single
copy-paste string that an instructor can email to a student.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from axiom.extensions.builtins.classroom.invite_token import (
    InvalidInviteError,
    InviteToken,
    create_invite_token,
    decode_invite,
    encode_invite,
    validate_invite_token,
)

# ---------------------------------------------------------------------------
# Structure + serialization
# ---------------------------------------------------------------------------


class TestInviteStructure:
    def test_create_produces_required_fields(self):
        invite = create_invite_token(
            classroom_id="ne101-prague-2026",
            coordinator_id="peer_node_abc123",
            ttl_hours=24,
        )
        assert invite.classroom_id == "ne101-prague-2026"
        assert invite.coordinator_id == "peer_node_abc123"
        assert invite.token  # non-empty secret
        assert invite.expires  # non-empty iso8601

    def test_token_secret_is_url_safe_32_bytes(self):
        invite = create_invite_token(
            classroom_id="c1", coordinator_id="n1", ttl_hours=1
        )
        # secrets.token_urlsafe(32) produces ~43 chars (base64 of 32 bytes).
        # Exact length varies with padding; lower bound is the point.
        assert len(invite.token) >= 40
        assert "=" not in invite.token  # url-safe encoding, no padding chars

    def test_ttl_sets_expiry_correctly(self):
        before = datetime.now(UTC)
        invite = create_invite_token(
            classroom_id="c1", coordinator_id="n1", ttl_hours=12
        )
        after = datetime.now(UTC)

        expires = datetime.fromisoformat(invite.expires)
        # expires should be within [before+12h, after+12h]
        assert expires >= before + timedelta(hours=12)
        assert expires <= after + timedelta(hours=12, seconds=1)

    def test_two_invites_have_distinct_tokens(self):
        a = create_invite_token("c", "n", 1)
        b = create_invite_token("c", "n", 1)
        assert a.token != b.token


class TestInviteCoding:
    def test_encode_decode_roundtrip(self):
        original = create_invite_token("c1", "n1", 24)
        encoded = encode_invite(original)
        decoded = decode_invite(encoded)
        assert decoded == original

    def test_encoded_is_single_string(self):
        invite = create_invite_token("c1", "n1", 24)
        encoded = encode_invite(invite)
        # Email/paste-friendly: no whitespace, no newlines.
        assert "\n" not in encoded
        assert " " not in encoded

    def test_decode_rejects_garbage(self):
        with pytest.raises(InvalidInviteError):
            decode_invite("this-is-not-a-valid-invite")

    def test_decode_rejects_empty(self):
        with pytest.raises(InvalidInviteError):
            decode_invite("")

    def test_decode_rejects_missing_fields(self):
        import base64
        import json

        # Valid base64, valid json, missing coordinator_id.
        partial = {"token": "x", "classroom_id": "c", "expires": "2026-01-01T00:00:00+00:00"}
        malformed = base64.urlsafe_b64encode(json.dumps(partial).encode()).decode()
        with pytest.raises(InvalidInviteError):
            decode_invite(malformed)


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


class TestValidation:
    def test_fresh_invite_validates(self):
        invite = create_invite_token("c1", "n1", ttl_hours=1)
        result = validate_invite_token(invite)
        assert result.valid is True
        assert result.reason is None

    def test_expired_invite_rejected(self):
        # Construct directly with past expiry; don't rely on time travel.
        past = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
        invite = InviteToken(
            token="abc",
            classroom_id="c1",
            coordinator_id="n1",
            expires=past,
        )
        result = validate_invite_token(invite)
        assert result.valid is False
        assert "expired" in result.reason.lower()

    def test_validation_accepts_explicit_now_for_testability(self):
        """Allow passing a fixed ``now`` so tests can exercise the
        boundary without freezing the clock."""
        invite = create_invite_token("c1", "n1", ttl_hours=1)
        future = datetime.now(UTC) + timedelta(hours=2)
        result = validate_invite_token(invite, now=future)
        assert result.valid is False
        assert "expired" in result.reason.lower()

    def test_validation_rejects_malformed_expires(self):
        invite = InviteToken(
            token="abc",
            classroom_id="c1",
            coordinator_id="n1",
            expires="not-an-iso8601-string",
        )
        result = validate_invite_token(invite)
        assert result.valid is False
        assert "expires" in result.reason.lower()
