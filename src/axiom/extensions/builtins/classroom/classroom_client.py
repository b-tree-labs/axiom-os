# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Student-side classroom-join client.

Tier A PR 5 — composes PRs 1-4 into one :class:`ClassroomJoinClient`
the CLI can call with ``(encoded_invite, student_id, coordinator_url)``
and get back a valid, persisted :class:`StoredMembership` or a
structured error.

Transport is injected via :class:`Transport` (any callable with
``.post(url, body) -> (status, body)``), so tests wire a direct
in-process call to :func:`coordinator_join_endpoint` and the real
HTTP adapter lives over `requests` / `http.client` without touching
ceremony logic.

**Response body format** (coordinator → student) is JSON::

    // 200 — accepted
    {"manifest": "<base64url-encoded manifest>",
     "coordinator_public_key": "<base64 pubkey>"}

    // 4xx — rejected
    {"error": "<human-readable reason>"}

The pubkey travels in the response so the student can verify the
returned manifest's signature without a separate identity-lookup
round trip. (A hardening PR layers TOFU on top: student records the
coordinator's pubkey on first join and rejects any subsequent join
where it silently changes.)
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Protocol

from axiom.vega.federation.identity import NodeIdentity

from .classroom_coordinator import (
    MembershipManifest,
    decode_membership_manifest,
    verify_membership_manifest,
)
from .classroom_join_request import (
    encode_join_request,
    sign_join_request,
)
from .invite_token import (
    InvalidInviteError,
    decode_invite,
    validate_invite_token,
)
from .student_membership import MembershipStore, StoredMembership

# ---------------------------------------------------------------------------
# Transport protocol
# ---------------------------------------------------------------------------


class Transport(Protocol):
    def post(self, url: str, body: str) -> tuple[int, str]: ...


# ---------------------------------------------------------------------------
# Exceptions + result
# ---------------------------------------------------------------------------


class JoinClientError(Exception):
    """Raised for client-side structural errors BEFORE any network call.

    Server-reported rejections return via :class:`JoinResult.error` so
    the CLI can surface the reason uniformly.
    """


@dataclass(frozen=True)
class JoinResult:
    accepted: bool
    membership: StoredMembership | None = None
    error: str | None = None


# ---------------------------------------------------------------------------
# The client
# ---------------------------------------------------------------------------


@dataclass
class ClassroomJoinClient:
    student_identity: NodeIdentity
    transport: Transport
    store: MembershipStore

    def join(
        self,
        *,
        encoded_invite: str,
        student_id: str,
        coordinator_url: str,
    ) -> JoinResult:
        """Run the ceremony end-to-end.

        Returns :class:`JoinResult` for both success and server-reported
        rejection. Raises :class:`JoinClientError` only for client-side
        structural problems (unparseable invite) so callers can't
        accidentally POST garbage.
        """
        # --- 1. Decode invite (fast-fail before any network) ---
        try:
            invite = decode_invite(encoded_invite)
        except InvalidInviteError as exc:
            raise JoinClientError(f"invalid invite: {exc}") from exc

        ttl = validate_invite_token(invite)
        if not ttl.valid:
            return JoinResult(accepted=False, error=f"invite expired: {ttl.reason}")

        # --- 2. Sign + encode join request ---
        request = sign_join_request(
            identity=self.student_identity,
            invite=invite,
            student_id=student_id,
        )
        body = encode_join_request(request)

        # --- 3. POST over the injected transport ---
        try:
            status, response_body = self.transport.post(coordinator_url, body)
        except Exception as exc:
            return JoinResult(
                accepted=False,
                error=f"transport error calling {coordinator_url}: {exc}",
            )

        # --- 4. Parse structured response ---
        try:
            response = json.loads(response_body) if response_body else {}
        except json.JSONDecodeError as exc:
            return JoinResult(
                accepted=False,
                error=f"coordinator returned non-JSON body: {exc}",
            )
        if not isinstance(response, dict):
            return JoinResult(
                accepted=False,
                error="coordinator response body is not a JSON object",
            )

        if status != 200:
            return JoinResult(
                accepted=False,
                error=response.get("error", f"coordinator refused (HTTP {status})"),
            )

        encoded_manifest = response.get("manifest")
        coordinator_public_key = response.get("coordinator_public_key")
        if not encoded_manifest or not coordinator_public_key:
            return JoinResult(
                accepted=False,
                error="coordinator response missing manifest or coordinator_public_key",
            )

        try:
            manifest: MembershipManifest = decode_membership_manifest(encoded_manifest)
        except ValueError as exc:
            return JoinResult(
                accepted=False,
                error=f"coordinator returned malformed manifest: {exc}",
            )

        # --- 5. Verify signature before trusting ---
        verify = verify_membership_manifest(
            manifest, coordinator_public_key=coordinator_public_key
        )
        if not verify.valid:
            return JoinResult(
                accepted=False,
                error=f"returned manifest failed signature check: {verify.reason}",
            )

        # --- 6. Persist locally ---
        self.store.save(manifest=manifest, coordinator_public_key=coordinator_public_key)
        stored = self.store.load(manifest.classroom_id)
        return JoinResult(accepted=True, membership=stored)


__all__ = [
    "ClassroomJoinClient",
    "JoinClientError",
    "JoinResult",
    "Transport",
]
