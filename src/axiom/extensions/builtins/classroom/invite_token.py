# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Classroom invite tokens — the opening move of the join ceremony.

An instructor generates an invite with :func:`create_invite_token`,
encodes it with :func:`encode_invite`, and emails the resulting string
to a student. The student runs::

    axi classroom join <encoded-invite>

which :func:`decode_invite` parses and :func:`validate_invite_token`
checks for TTL expiry. Subsequent Tier A work layers an A2A handshake
on top — this module is just the carrier.

Format (per ``tier-a-federation-ceremony-roadmap.md``)::

    {"token": <url-safe-base64>,   # one-time secret
     "classroom_id": <uuid>,        # which cohort
     "coordinator_id": <node_id>,   # who signs the manifest
     "expires": <iso8601>}          # TTL

Encoded as ``base64url(json(payload))`` so the whole thing is a single
copy-paste-safe string.
"""

from __future__ import annotations

import base64
import binascii
import json
import secrets
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta

# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class InviteToken:
    """Decoded invite. Immutable so callers can't mutate in-flight."""

    token: str
    classroom_id: str
    coordinator_id: str
    expires: str  # ISO 8601 with timezone
    # Optional. When set, students don't need `--coordinator URL` on the CLI —
    # the full ceremony can run from the encoded invite alone. Backward
    # compatible: older coordinators that didn't set this field still
    # produce valid invites, and decoders default to None.
    coordinator_url: str | None = None


@dataclass(frozen=True)
class ValidationResult:
    valid: bool
    reason: str | None = None


class InvalidInviteError(ValueError):
    """Raised when an encoded invite can't be decoded or is structurally wrong."""


# ---------------------------------------------------------------------------
# Construction
# ---------------------------------------------------------------------------


# Keep in sync with `decode_invite` — missing any of these triggers
# InvalidInviteError. Order preserved for stable serialization.
_REQUIRED_FIELDS = ("token", "classroom_id", "coordinator_id", "expires")


def create_invite_token(
    classroom_id: str,
    coordinator_id: str,
    ttl_hours: int,
    coordinator_url: str | None = None,
) -> InviteToken:
    """Mint a fresh invite with a cryptographically random one-time secret.

    The caller is responsible for persisting the ``token`` on the
    coordinator side so subsequent join requests can be verified + marked
    consumed. This function only constructs the envelope.

    Setting ``coordinator_url`` is strongly recommended — it lets students
    run ``axi classroom join <invite>`` without having to paste a URL
    separately. Omitted only for callers that specifically want the
    student to provide the URL out-of-band.
    """
    token = secrets.token_urlsafe(32)
    expires = datetime.now(UTC) + timedelta(hours=ttl_hours)
    return InviteToken(
        token=token,
        classroom_id=classroom_id,
        coordinator_id=coordinator_id,
        expires=expires.isoformat(),
        coordinator_url=coordinator_url,
    )


# ---------------------------------------------------------------------------
# Serialization
# ---------------------------------------------------------------------------


def encode_invite(invite: InviteToken) -> str:
    """Base64url-encode the JSON payload.

    Produces a single whitespace-free string suitable for pasting into
    an email, a chat message, or a CLI argument. Round-trips via
    :func:`decode_invite`.
    """
    payload = json.dumps(asdict(invite), separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(payload).rstrip(b"=").decode("ascii")


def decode_invite(encoded: str) -> InviteToken:
    """Reverse :func:`encode_invite`.

    Raises :class:`InvalidInviteError` on any structural problem:
    empty input, non-base64, invalid JSON, missing required field.
    """
    if not encoded:
        raise InvalidInviteError("empty invite")

    # base64url without padding — restore it so urlsafe_b64decode accepts.
    padded = encoded + "=" * (-len(encoded) % 4)
    try:
        raw = base64.urlsafe_b64decode(padded.encode("ascii"))
    except (binascii.Error, ValueError, UnicodeEncodeError) as exc:
        raise InvalidInviteError(f"not valid base64url: {exc}") from exc

    try:
        payload = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        # A valid base64url string can decode to non-UTF-8 bytes (e.g.
        # "this-is-not-a-valid-invite" is valid base64 but its bytes aren't
        # utf-8). Treat both error classes as "not a valid JSON envelope."
        raise InvalidInviteError(f"not valid JSON: {exc}") from exc

    if not isinstance(payload, dict):
        raise InvalidInviteError("payload is not a JSON object")

    missing = [f for f in _REQUIRED_FIELDS if f not in payload]
    if missing:
        raise InvalidInviteError(f"missing required field(s): {', '.join(missing)}")

    # Ignore extra fields so we can extend the envelope later without
    # breaking existing clients. Only the REQUIRED_FIELDS feed the model.
    coordinator_url = payload.get("coordinator_url")
    return InviteToken(
        token=str(payload["token"]),
        classroom_id=str(payload["classroom_id"]),
        coordinator_id=str(payload["coordinator_id"]),
        expires=str(payload["expires"]),
        coordinator_url=str(coordinator_url) if coordinator_url else None,
    )


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def validate_invite_token(
    invite: InviteToken,
    *,
    now: datetime | None = None,
) -> ValidationResult:
    """Check that the invite is temporally + structurally valid.

    ``now`` is injectable for deterministic tests. In production, callers
    should leave it as None (defaults to ``datetime.now(UTC)``).

    Note: this does NOT verify that the ``token`` matches anything on
    the coordinator — that's a separate authenticity check performed
    during the A2A handshake (future PR). This function only catches
    expired + structurally-broken envelopes.
    """
    check_time = now if now is not None else datetime.now(UTC)

    try:
        expires = datetime.fromisoformat(invite.expires)
    except ValueError as exc:
        return ValidationResult(valid=False, reason=f"expires field unparseable: {exc}")

    # Normalize: if the caller passed a naive datetime, assume UTC.
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=UTC)
    if check_time.tzinfo is None:
        check_time = check_time.replace(tzinfo=UTC)

    if check_time >= expires:
        return ValidationResult(
            valid=False,
            reason=f"invite expired at {invite.expires} (now: {check_time.isoformat()})",
        )

    return ValidationResult(valid=True, reason=None)


__all__ = [
    "InvalidInviteError",
    "InviteToken",
    "ValidationResult",
    "create_invite_token",
    "decode_invite",
    "encode_invite",
    "validate_invite_token",
]
