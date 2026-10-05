# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Signing grants (ADR-146).

A grant is the credential a browser signs with. It is minted for one
presentation after the person answered it, and binds the digest, the person,
how and when they authenticated, and the device and console. It lives at most
:data:`MAX_TTL_SECONDS` and signs once.

The token is the grant's canonical JSON and the node's signature over
:data:`GRANT_DOMAIN` followed by those bytes, each base64url-encoded and joined
with a dot. Only the node that minted it can have signed it, and the prefix
keeps grant signatures apart from record and anchor signatures.

Minting checks what the logbook asks of the person and device:

- the posture floor for the type (``open`` and ``service`` never sign);
- ``fresh_within``: the person authenticated recently enough; an unknown
  sign-in time fails (``reauth_required``);
- the device class may sign this type (a phone may not, by default);
- ``presence``: refused until presence can be proven for the location
  (``presence_required``).

:func:`axiom.extensions.builtins.attest.service.sign` verifies the token and
records its first use; a second use is refused.
"""

from __future__ import annotations

import base64
import json
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from axiom.attest.canonical import canonical_bytes, normalise
from axiom.attest.chain import Signer
from axiom.infra.principal import PrincipalContext
from axiom.vega.identity.keypair import verify as ed25519_verify

from . import devices, presence
from .logbooks import SIGNING_POSTURES
from .service import AttestRefused, presentation_context

GRANT_DOMAIN: bytes = b"axiom/attest/grant/v1\n"
MAX_TTL_SECONDS = 60


class GrantRefused(AttestRefused):
    """No grant: the person, their sign-in or their device does not meet the logbook.

    ``code`` says which, for a surface to act on: ``reauth_required`` (sign in
    again within ``max_age`` seconds), ``presence_required``, ``device_not_allowed``,
    ``posture``, ``not_for_you``, ``not_open``, ``invalid`` or ``expired``."""

    def __init__(self, message: str, *, code: str = "refused", max_age: int | None = None):
        super().__init__(message)
        self.code = code
        self.max_age = max_age


@dataclass(frozen=True)
class Authentication:
    """How the person is signed in, as the session or IdP states it."""

    principal: PrincipalContext
    auth_time: datetime | None
    amr: tuple[str, ...] = ()


@dataclass(frozen=True)
class Device:
    """The device asking, as enrolled with the node (ADR-146)."""

    device_id: str
    device_class: str
    console_id: str | None = None
    location: str | None = None


def _now() -> datetime:
    return datetime.now(UTC)


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def mint(
    presentation_id: str,
    *,
    auth: Authentication,
    device: Device,
    signer: Signer,
    presence_code: str | None = None,
    ttl: int = MAX_TTL_SECONDS,
) -> str:
    """Return a grant token, or raise :class:`GrantRefused`.

    ``device`` names the device and console asking. Its class and location
    are taken from enrolment at the draft's site; an unenrolled device is a
    personal session with no location, whatever the caller said."""
    if not 0 < ttl <= MAX_TTL_SECONDS:
        raise ValueError(f"a grant lives at most {MAX_TTL_SECONDS} seconds")
    ctx = presentation_context(presentation_id)
    et = ctx["entry_type"]
    handle = auth.principal.handle
    if ctx["status"] != "open":
        raise GrantRefused(f"draft {ctx['draft_id']} is already {ctx['status']}", code="not_open")
    if handle != ctx["for_principal"]:
        raise GrantRefused(
            f"draft {ctx['draft_id']} is for {ctx['for_principal']}, not {handle}",
            code="not_for_you",
        )
    posture = auth.principal.posture
    if posture not in SIGNING_POSTURES or not auth.principal.meets(et.posture):
        raise GrantRefused(
            f"posture {posture!r} cannot sign; it needs {et.posture!r} or higher", code="posture"
        )
    enrolled = devices.get(device.device_id, ctx["site_id"])
    device_class = enrolled.device_class if enrolled else "personal"
    location = enrolled.location if enrolled else None
    now = _now()
    fresh_met = None
    if et.fresh_within_seconds is not None:
        window = timedelta(seconds=et.fresh_within_seconds)
        fresh_met = auth.auth_time is not None and now - auth.auth_time <= window
        if not fresh_met:
            raise GrantRefused(
                f"reauth_required: {ctx['logbook']}.{et.id} needs a sign-in within "
                f"{et.fresh_within_seconds}s",
                code="reauth_required",
                max_age=et.fresh_within_seconds,
            )
    if device_class not in et.sign_devices:
        raise GrantRefused(
            f"a {device_class} may not sign {ctx['logbook']}.{et.id} "
            f"(allowed: {list(et.sign_devices)})",
            code="device_not_allowed",
        )
    proof = None
    if et.presence is not None:
        if location != et.presence:
            raise GrantRefused(
                f"presence_required: {ctx['logbook']}.{et.id} must be signed on a device "
                f"enrolled at {et.presence!r}",
                code="presence_required",
            )
        if enrolled is not None and enrolled.mobility == "fixed":
            proof = "enrolment"
        elif presence_code and presence.verify_code(
            ctx["site_id"], et.presence, presence_code, now=now
        ):
            proof = "location_code"
        else:
            raise GrantRefused(
                f"presence_required: enter the code shown at {et.presence!r} to sign here",
                code="presence_required",
            )
    claims = normalise(
        {
            "grant_id": str(uuid.uuid4()),
            "presentation_id": presentation_id,
            "digest": ctx["digest"],
            "logbook": ctx["logbook"],
            "meaning": ctx["meaning"],
            "principal": handle,
            "posture": posture,
            "idp": auth.principal.idp,
            "auth_time": auth.auth_time,
            "amr": list(auth.amr),
            "fresh_within_met": fresh_met,
            "device_id": device.device_id,
            "device_class": device_class,
            "console_id": device.console_id,
            "location": location,
            "presence": {"location": et.presence, "proof": proof} if proof else None,
            "issued_at": now,
            "expires_at": now + timedelta(seconds=ttl),
        }
    )
    body = canonical_bytes(claims)
    return f"{_b64(body)}.{_b64(signer.sign(GRANT_DOMAIN + body))}"


def read(token: str, public_keys: Mapping[str, bytes]) -> dict[str, Any]:
    """Verify a token's signature and expiry and return its claims."""
    try:
        body_b64, sig_b64 = token.split(".")
        body, sig = _unb64(body_b64), _unb64(sig_b64)
        claims = json.loads(body)
    except (ValueError, TypeError):
        raise GrantRefused("the grant is malformed", code="invalid") from None
    if canonical_bytes(claims) != body:
        raise GrantRefused("the grant is not in canonical form", code="invalid")
    good = False
    for public in public_keys.values():
        try:
            good = ed25519_verify(public, GRANT_DOMAIN + body, sig)
        except ValueError:
            good = False
        if good:
            break
    if not good:
        raise GrantRefused("the grant's signature does not verify", code="invalid")
    expires = datetime.fromisoformat(claims["expires_at"].replace("Z", "+00:00"))
    if _now() > expires:
        raise GrantRefused("the grant expired; request a new one", code="expired")
    return claims


__all__ = [
    "Authentication",
    "Device",
    "GRANT_DOMAIN",
    "GrantRefused",
    "MAX_TTL_SECONDS",
    "mint",
    "read",
]
