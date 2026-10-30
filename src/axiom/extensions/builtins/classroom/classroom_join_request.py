# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Classroom join-request — signed payload the student sends to the coordinator.

Tier A PR 2 on the Prague classroom federation ceremony. Sits between
the invite-token envelope (PR 1) and the A2A handshake endpoint (PR 3).

Flow:
  1. Instructor mints an invite → student receives encoded invite string.
  2. Student decodes the invite, then calls :func:`sign_join_request` with
     their NodeIdentity → produces a `ClassroomJoinRequest` signed with
     their Ed25519 private key.
  3. (Future PR) Student POSTs the encoded request to the coordinator's
     ``/classroom/join`` endpoint.
  4. Coordinator :func:`verify_join_request` — checks signature is valid
     against the embedded public_key AND that the classroom_id matches
     the coordinator's own cohort.

The signature covers a **canonical** JSON serialization — sorted keys,
compact separators, UTF-8 — so the student and coordinator produce
byte-identical payloads to hash. Any tampering (changing student_id,
classroom_id, etc.) breaks the signature.

What this module does NOT do:
  - Verify that ``invite_token`` matches one the coordinator issued —
    that's a coordinator-side lookup layered on in PR 3.
  - Verify the invite's TTL — callers should :func:`validate_invite_token`
    first at both ends. This module only covers authenticity.
  - Persist membership on the coordinator side — PR 4.
"""

from __future__ import annotations

import base64
import binascii
import json
from dataclasses import asdict, dataclass

from axiom.vega.federation.identity import NodeIdentity

from .invite_token import InviteToken

# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ClassroomJoinRequest:
    """Signed request the student sends to the classroom coordinator."""

    student_id: str
    member_node: str  # the student's node_id (sha256(public_key)[:16])
    public_key: str  # base64(raw Ed25519 pubkey), used for sig verification
    invite_token: str  # the opaque secret from the invite envelope
    classroom_id: str
    signature: str  # base64 Ed25519 signature over canonical payload


@dataclass(frozen=True)
class VerifyResult:
    valid: bool
    reason: str | None = None


class InvalidJoinRequestError(ValueError):
    """Raised when an encoded join request can't be decoded or is structurally wrong."""


_REQUIRED_FIELDS = (
    "student_id",
    "member_node",
    "public_key",
    "invite_token",
    "classroom_id",
    "signature",
)

# Fields that go into the signed-over canonical form. The signature itself
# is NEVER part of what's being signed.
_SIGNED_FIELDS = (
    "student_id",
    "member_node",
    "public_key",
    "invite_token",
    "classroom_id",
)


# ---------------------------------------------------------------------------
# Canonical payload for signing
# ---------------------------------------------------------------------------


def canonical_signing_payload(request: ClassroomJoinRequest) -> bytes:
    """Produce deterministic bytes that the signature covers.

    Keys sorted alphabetically, compact separators, UTF-8. Any two
    requests with identical ``_SIGNED_FIELDS`` values produce identical
    bytes regardless of construction order — essential so the student
    and coordinator compute byte-identical payloads.
    """
    data = {f: getattr(request, f) for f in _SIGNED_FIELDS}
    return json.dumps(data, sort_keys=True, separators=(",", ":")).encode("utf-8")


# ---------------------------------------------------------------------------
# Signing
# ---------------------------------------------------------------------------


def _load_private_key_from_identity(identity: NodeIdentity):
    """Load the Ed25519 private key referenced by ``identity.private_key_path``."""
    from cryptography.hazmat.primitives.serialization import load_pem_private_key

    priv_bytes = identity.private_key_path.read_bytes()
    return load_pem_private_key(priv_bytes, password=None)


def sign_join_request(
    identity: NodeIdentity,
    invite: InviteToken,
    student_id: str,
) -> ClassroomJoinRequest:
    """Construct + sign a join request on behalf of ``identity``.

    The ``student_id`` is a classroom-scoped handle (``"alice"``,
    ``"ben@ut"``, etc.) chosen by the instructor during enrollment —
    NOT the node_id. Both travel in the request.
    """
    unsigned = ClassroomJoinRequest(
        student_id=student_id,
        member_node=identity.node_id,
        public_key=identity.public_key,
        invite_token=invite.token,
        classroom_id=invite.classroom_id,
        signature="",
    )
    payload = canonical_signing_payload(unsigned)

    priv = _load_private_key_from_identity(identity)
    sig_bytes = priv.sign(payload)
    signature_b64 = base64.b64encode(sig_bytes).decode("ascii")

    return ClassroomJoinRequest(
        student_id=unsigned.student_id,
        member_node=unsigned.member_node,
        public_key=unsigned.public_key,
        invite_token=unsigned.invite_token,
        classroom_id=unsigned.classroom_id,
        signature=signature_b64,
    )


# ---------------------------------------------------------------------------
# Verification
# ---------------------------------------------------------------------------


def verify_join_request(
    request: ClassroomJoinRequest,
    expected_classroom_id: str,
) -> VerifyResult:
    """Verify the Ed25519 signature + confirm classroom scope.

    The coordinator calls this after decoding a wire request. If the
    signature verifies AND ``request.classroom_id == expected_classroom_id``,
    the request is authentic for this cohort.

    Additional checks (invite-token freshness, token-not-yet-consumed,
    matching student enrollment) are done by the coordinator separately.
    """
    if request.classroom_id != expected_classroom_id:
        return VerifyResult(
            valid=False,
            reason=(
                f"classroom mismatch: request claims {request.classroom_id!r} "
                f"but coordinator expects {expected_classroom_id!r}"
            ),
        )

    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives.asymmetric.ed25519 import (
        Ed25519PublicKey,
    )

    try:
        pub_bytes = base64.b64decode(request.public_key)
    except (binascii.Error, ValueError) as exc:
        return VerifyResult(valid=False, reason=f"public_key not base64: {exc}")

    try:
        pub_key = Ed25519PublicKey.from_public_bytes(pub_bytes)
    except ValueError as exc:
        return VerifyResult(valid=False, reason=f"public_key not valid Ed25519: {exc}")

    try:
        sig_bytes = base64.b64decode(request.signature)
    except (binascii.Error, ValueError) as exc:
        return VerifyResult(valid=False, reason=f"signature not base64: {exc}")

    payload = canonical_signing_payload(request)
    try:
        pub_key.verify(sig_bytes, payload)
    except InvalidSignature:
        return VerifyResult(
            valid=False,
            reason="signature verification failed (payload tampered or wrong key)",
        )
    except Exception as exc:  # defensive — Ed25519 verify should only raise InvalidSignature
        return VerifyResult(valid=False, reason=f"signature check error: {exc}")

    return VerifyResult(valid=True)


# ---------------------------------------------------------------------------
# Wire format
# ---------------------------------------------------------------------------


def encode_join_request(request: ClassroomJoinRequest) -> str:
    """Base64url(json(request)) — a single copy-paste-safe string."""
    payload = json.dumps(asdict(request), separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(payload).rstrip(b"=").decode("ascii")


def decode_join_request(encoded: str) -> ClassroomJoinRequest:
    """Reverse :func:`encode_join_request`.

    Raises :class:`InvalidJoinRequestError` on structural problems.
    """
    if not encoded:
        raise InvalidJoinRequestError("empty request")

    padded = encoded + "=" * (-len(encoded) % 4)
    try:
        raw = base64.urlsafe_b64decode(padded.encode("ascii"))
    except (binascii.Error, ValueError, UnicodeEncodeError) as exc:
        raise InvalidJoinRequestError(f"not valid base64url: {exc}") from exc

    try:
        payload = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise InvalidJoinRequestError(f"not valid JSON: {exc}") from exc

    if not isinstance(payload, dict):
        raise InvalidJoinRequestError("payload is not a JSON object")

    missing = [f for f in _REQUIRED_FIELDS if f not in payload]
    if missing:
        raise InvalidJoinRequestError(
            f"missing required field(s): {', '.join(missing)}"
        )

    return ClassroomJoinRequest(
        student_id=str(payload["student_id"]),
        member_node=str(payload["member_node"]),
        public_key=str(payload["public_key"]),
        invite_token=str(payload["invite_token"]),
        classroom_id=str(payload["classroom_id"]),
        signature=str(payload["signature"]),
    )


__all__ = [
    "ClassroomJoinRequest",
    "InvalidJoinRequestError",
    "VerifyResult",
    "canonical_signing_payload",
    "decode_join_request",
    "encode_join_request",
    "sign_join_request",
    "verify_join_request",
]
