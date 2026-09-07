# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Coordinator-side classroom-join handler — the decisive step of the ceremony.

Tier A PR 3 (logic-only). Composes the invite-token envelope (PR 1) +
join-request signing (PR 2) into a single pure function that validates,
updates cohort state, and issues a signed membership manifest.

    Student                                          Coordinator (peer node)
    -------                                          --------------------
    encode_join_request(signed) ─HTTP POST─▶   process_join_request()
                                                    ├─ decode_join_request
                                                    ├─ invite_registry.find_by_token
                                                    ├─ validate_invite_token (TTL)
                                                    ├─ invite_registry.is_consumed?
                                                    ├─ verify_join_request (sig)
                                                    ├─ add_member to cohort
                                                    ├─ invite_registry.mark_consumed
                                                    └─ sign_membership_manifest
    decode_membership_manifest  ◀──HTTP 200──  encode_membership_manifest
    verify_membership_manifest

No HTTP here yet — :func:`process_join_request` is a pure function. A thin
A2A wrapper (next PR) turns it into an HTTP endpoint.

``InMemoryInviteRegistry`` is a minimal store suitable for tests +
single-process demos. A disk-backed registry that survives coordinator
restarts can slot in behind the same ``InviteRegistry``-shaped protocol.
"""

from __future__ import annotations

import base64
import binascii
import json
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Protocol

from axiom.vega.federation.identity import NodeIdentity

from .classroom_federation import ClassroomCohort, add_member
from .classroom_join_request import (
    InvalidJoinRequestError,
    decode_join_request,
    verify_join_request,
)
from .invite_token import InviteToken, validate_invite_token

# ---------------------------------------------------------------------------
# Invite registry protocol + in-memory impl
# ---------------------------------------------------------------------------


class InviteRegistry(Protocol):
    """Minimum contract a coordinator-side invite store must satisfy."""

    def register(self, invite: InviteToken) -> None: ...
    def find_by_token(self, token: str) -> InviteToken | None: ...
    def is_consumed(self, token: str) -> bool: ...
    def mark_consumed(self, token: str) -> None: ...


@dataclass
class InMemoryInviteRegistry:
    """Minimum viable registry — fine for tests + single-process demos.

    A persistent registry (SQLite, Postgres, disk JSONL) can implement
    the same :class:`InviteRegistry` protocol without changing
    :func:`process_join_request`.
    """

    _invites: dict[str, InviteToken] = field(default_factory=dict)
    _consumed: set[str] = field(default_factory=set)

    def register(self, invite: InviteToken) -> None:
        self._invites[invite.token] = invite

    def find_by_token(self, token: str) -> InviteToken | None:
        return self._invites.get(token)

    def is_consumed(self, token: str) -> bool:
        return token in self._consumed

    def mark_consumed(self, token: str) -> None:
        self._consumed.add(token)


# ---------------------------------------------------------------------------
# Membership manifest — signed by coordinator, returned to student
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MembershipManifest:
    """Coordinator's signed attestation that the student is a cohort member."""

    classroom_id: str
    student_id: str
    member_node: str
    coordinator_node: str
    status: str  # ACTIVE / QUARANTINED / REVOKED — snapshot at issue time
    joined_at: str  # ISO 8601 with timezone
    signature: str  # base64 Ed25519 signature over canonical payload


@dataclass(frozen=True)
class ManifestVerifyResult:
    valid: bool
    reason: str | None = None


# Which fields contribute to the signed-over bytes. ``signature`` itself
# is NEVER included (same rule as the student-side request).
_MANIFEST_SIGNED_FIELDS = (
    "classroom_id",
    "student_id",
    "member_node",
    "coordinator_node",
    "status",
    "joined_at",
)

_MANIFEST_REQUIRED_FIELDS = (*_MANIFEST_SIGNED_FIELDS, "signature")


def _manifest_canonical_payload(manifest: MembershipManifest) -> bytes:
    data = {f: getattr(manifest, f) for f in _MANIFEST_SIGNED_FIELDS}
    return json.dumps(data, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _load_private_key(identity: NodeIdentity):
    from cryptography.hazmat.primitives.serialization import load_pem_private_key

    priv_bytes = identity.private_key_path.read_bytes()
    return load_pem_private_key(priv_bytes, password=None)


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def sign_membership_manifest(
    *,
    identity: NodeIdentity,
    cohort: ClassroomCohort,
    student_id: str,
) -> MembershipManifest:
    """Construct + sign a membership manifest for ``student_id`` in ``cohort``.

    The student must already be a cohort member; caller typically runs
    :func:`classroom_federation.add_member` immediately before this.
    """
    target = None
    for m in cohort.members:
        if m.student_id == student_id:
            target = m
            break
    if target is None:
        raise ValueError(
            f"student {student_id!r} is not a member of cohort {cohort.classroom_id!r}"
        )

    unsigned = MembershipManifest(
        classroom_id=cohort.classroom_id,
        student_id=student_id,
        member_node=target.member_node,
        coordinator_node=cohort.coordinator_node,
        status=target.status,
        joined_at=target.joined_at or _now_iso(),
        signature="",
    )
    payload = _manifest_canonical_payload(unsigned)

    priv = _load_private_key(identity)
    sig_bytes = priv.sign(payload)
    signature = base64.b64encode(sig_bytes).decode("ascii")

    return MembershipManifest(
        classroom_id=unsigned.classroom_id,
        student_id=unsigned.student_id,
        member_node=unsigned.member_node,
        coordinator_node=unsigned.coordinator_node,
        status=unsigned.status,
        joined_at=unsigned.joined_at,
        signature=signature,
    )


def verify_membership_manifest(
    manifest: MembershipManifest,
    *,
    coordinator_public_key: str,
) -> ManifestVerifyResult:
    """Verify the coordinator's signature on the manifest."""
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives.asymmetric.ed25519 import (
        Ed25519PublicKey,
    )

    try:
        pub_bytes = base64.b64decode(coordinator_public_key)
    except (binascii.Error, ValueError) as exc:
        return ManifestVerifyResult(valid=False, reason=f"coord pubkey not base64: {exc}")

    try:
        pub_key = Ed25519PublicKey.from_public_bytes(pub_bytes)
    except ValueError as exc:
        return ManifestVerifyResult(valid=False, reason=f"coord pubkey not Ed25519: {exc}")

    try:
        sig_bytes = base64.b64decode(manifest.signature)
    except (binascii.Error, ValueError) as exc:
        return ManifestVerifyResult(valid=False, reason=f"signature not base64: {exc}")

    payload = _manifest_canonical_payload(manifest)
    try:
        pub_key.verify(sig_bytes, payload)
    except InvalidSignature:
        return ManifestVerifyResult(
            valid=False,
            reason="signature verification failed (manifest tampered or wrong coord key)",
        )

    return ManifestVerifyResult(valid=True)


# ---------------------------------------------------------------------------
# Manifest wire format
# ---------------------------------------------------------------------------


def encode_membership_manifest(manifest: MembershipManifest) -> str:
    payload = json.dumps(asdict(manifest), separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(payload).rstrip(b"=").decode("ascii")


def decode_membership_manifest(encoded: str) -> MembershipManifest:
    if not encoded:
        raise ValueError("empty manifest")

    padded = encoded + "=" * (-len(encoded) % 4)
    try:
        raw = base64.urlsafe_b64decode(padded.encode("ascii"))
    except (binascii.Error, ValueError, UnicodeEncodeError) as exc:
        raise ValueError(f"manifest not valid base64url: {exc}") from exc

    try:
        payload = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ValueError(f"manifest not valid JSON: {exc}") from exc

    if not isinstance(payload, dict):
        raise ValueError("manifest payload is not a JSON object")

    missing = [f for f in _MANIFEST_REQUIRED_FIELDS if f not in payload]
    if missing:
        raise ValueError(f"manifest missing field(s): {', '.join(missing)}")

    return MembershipManifest(
        classroom_id=str(payload["classroom_id"]),
        student_id=str(payload["student_id"]),
        member_node=str(payload["member_node"]),
        coordinator_node=str(payload["coordinator_node"]),
        status=str(payload["status"]),
        joined_at=str(payload["joined_at"]),
        signature=str(payload["signature"]),
    )


# ---------------------------------------------------------------------------
# The ceremony — pure function
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ProcessResult:
    """Outcome of :func:`process_join_request`."""

    accepted: bool
    manifest: MembershipManifest | None = None
    updated_cohort: ClassroomCohort | None = None
    error: str | None = None


def process_join_request(
    *,
    encoded_request: str,
    coordinator_identity: NodeIdentity,
    cohort: ClassroomCohort,
    invite_registry: InviteRegistry,
) -> ProcessResult:
    """Run the coordinator-side ceremony as a pure function.

    Given the student's encoded + signed join request, produce either a
    signed membership manifest (on success) or an error string (on any
    rejection). Does not mutate ``cohort`` — returns the updated cohort
    in :attr:`ProcessResult.updated_cohort` on success.

    Validation order (shortest-path rejection first):
        1. Structural decode
        2. Invite-token registered with this coordinator
        3. Invite-token not yet consumed (token-reuse defense)
        4. Invite-token not expired (TTL)
        5. Classroom scope matches
        6. Ed25519 signature over canonical request payload verifies
        7. ...add member, mark invite consumed, sign manifest, return.

    Callers: HTTP handler in a future PR maps ProcessResult → 200 / 4xx.
    """
    try:
        request = decode_join_request(encoded_request)
    except InvalidJoinRequestError as exc:
        return ProcessResult(accepted=False, error=f"malformed join request: {exc}")

    invite = invite_registry.find_by_token(request.invite_token)
    if invite is None:
        return ProcessResult(
            accepted=False,
            error="invite token not recognized by this coordinator",
        )

    if invite_registry.is_consumed(request.invite_token):
        return ProcessResult(
            accepted=False,
            error="invite token already consumed (reuse refused)",
        )

    ttl_check = validate_invite_token(invite)
    if not ttl_check.valid:
        return ProcessResult(
            accepted=False,
            error=f"invite expired: {ttl_check.reason}",
        )

    sig_check = verify_join_request(
        request, expected_classroom_id=cohort.classroom_id
    )
    if not sig_check.valid:
        return ProcessResult(
            accepted=False,
            error=f"join request rejected: {sig_check.reason}",
        )

    # All checks green — commit cohort update + mark invite consumed + sign.
    updated_cohort = add_member(
        cohort,
        student_id=request.student_id,
        member_node=request.member_node,
        invite_token=request.invite_token,
    )
    invite_registry.mark_consumed(request.invite_token)

    manifest = sign_membership_manifest(
        identity=coordinator_identity,
        cohort=updated_cohort,
        student_id=request.student_id,
    )

    return ProcessResult(
        accepted=True,
        manifest=manifest,
        updated_cohort=updated_cohort,
    )


# ---------------------------------------------------------------------------
# HTTP-adapter-ready endpoint function
# ---------------------------------------------------------------------------


def coordinator_join_endpoint(
    *,
    encoded_request: str,
    coordinator_identity: NodeIdentity,
    cohort: ClassroomCohort,
    invite_registry: InviteRegistry,
) -> tuple[int, str, ClassroomCohort | None]:
    """Pure-function HTTP-ish endpoint: request body → ``(status, body, updated_cohort)``.

    Response body is JSON:
      - 200: ``{"manifest": <encoded>, "coordinator_public_key": <base64>}``
      - 400: ``{"error": <reason>}``

    A thin adapter over ``http.server.BaseHTTPRequestHandler`` (not in
    this PR) maps ``(status, body)`` onto the wire. Tests drive this
    function directly without starting a server.
    """
    result = process_join_request(
        encoded_request=encoded_request,
        coordinator_identity=coordinator_identity,
        cohort=cohort,
        invite_registry=invite_registry,
    )
    if not result.accepted:
        error_body = json.dumps({"error": result.error or "rejected"})
        return 400, error_body, None

    payload = {
        "manifest": encode_membership_manifest(result.manifest),
        "coordinator_public_key": coordinator_identity.public_key,
    }
    return 200, json.dumps(payload), result.updated_cohort


__all__ = [
    "InMemoryInviteRegistry",
    "InviteRegistry",
    "ManifestVerifyResult",
    "MembershipManifest",
    "ProcessResult",
    "coordinator_join_endpoint",
    "decode_membership_manifest",
    "encode_membership_manifest",
    "process_join_request",
    "sign_membership_manifest",
    "verify_membership_manifest",
]
