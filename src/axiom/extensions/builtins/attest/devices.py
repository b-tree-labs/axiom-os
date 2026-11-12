# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Enrolled signing devices (ADR-146).

An administrator enrols a device with the site: its class (personal, kiosk,
tablet, phone), the location it is at, and whether it is fixed there or
portable. A grant takes these facts from enrolment, never from the caller, so
a browser cannot declare itself a fixed console in the room. A device that is
not enrolled is a personal session with no location.

Enrolling is administration, not a logbook act: it grants no authority to sign.
Retiring a device keeps the row with ``retired_at`` set.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import secrets
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from axiom.attest.canonical import canonical_bytes, normalise
from axiom.attest.chain import Signer
from axiom.vega.identity.keypair import verify as ed25519_verify

from . import store
from .db_models import AttestDevice
from .logbooks import DEVICE_CLASSES

MOBILITY = ("fixed", "portable")

#: How long an enrolment's one-time claim code lasts.
CLAIM_TTL = timedelta(minutes=15)
DEVICE_DOMAIN: bytes = b"axiom/attest/device/v1\n"


def _now() -> datetime:
    return datetime.now(UTC)


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _hash(code: str) -> str:
    return hashlib.sha256(code.encode("utf-8")).hexdigest()


_LOCATION = re.compile(r"^[a-z0-9_]+$")


@dataclass(frozen=True)
class Enrolment:
    device_id: str
    site_id: str
    device_class: str
    location: str | None
    mobility: str


def enroll(
    *,
    site_id: str,
    device_id: str,
    device_class: str,
    location: str | None,
    mobility: str,
    by: str,
) -> Enrolment:
    if device_class not in DEVICE_CLASSES:
        raise ValueError(f"device class {device_class!r} is not one of {list(DEVICE_CLASSES)}")
    if mobility not in MOBILITY:
        raise ValueError(f"mobility {mobility!r} is not one of {list(MOBILITY)}")
    if location is not None and not _LOCATION.match(location):
        raise ValueError(f"location {location!r} must be an id, [a-z0-9_]+")
    with store.session_scope() as s:
        row = s.get(AttestDevice, device_id)
        if row is not None and row.retired_at is None:
            raise ValueError(f"device {device_id} is already enrolled; retire it first")
        if row is None:
            row = AttestDevice(device_id=device_id)
            s.add(row)
        row.site_id = site_id
        row.device_class = device_class
        row.location = location
        row.mobility = mobility
        row.enrolled_by = by
        row.enrolled_at = datetime.now(UTC)
        row.retired_at = None
        row.retired_by = None
        s.commit()
    return Enrolment(device_id, site_id, device_class, location, mobility)


def retire(device_id: str, *, by: str) -> None:
    with store.session_scope() as s:
        row = s.get(AttestDevice, device_id)
        if row is None or row.retired_at is not None:
            raise ValueError(f"device {device_id} is not enrolled")
        row.retired_at = datetime.now(UTC)
        row.retired_by = by
        s.commit()


def get(device_id: str, site_id: str) -> Enrolment | None:
    """The device's current enrolment at ``site_id``, or ``None``."""
    with store.session_scope() as s:
        row = s.get(AttestDevice, device_id)
        if row is None or row.retired_at is not None or row.site_id != site_id:
            return None
        return Enrolment(row.device_id, row.site_id, row.device_class, row.location, row.mobility)


def issue_claim_code(device_id: str) -> str:
    """A one-time code the enrolled device's browser redeems to prove it is
    that device. Only its hash is stored; it lasts :data:`CLAIM_TTL`. Issuing
    a new one replaces any earlier code."""
    code = secrets.token_urlsafe(9)
    with store.session_scope() as s:
        row = s.get(AttestDevice, device_id)
        if row is None or row.retired_at is not None:
            raise ValueError(f"device {device_id} is not enrolled")
        row.claim_hash = _hash(code)
        row.claim_expires_at = _now() + CLAIM_TTL
        s.commit()
    return code


def redeem_claim(device_id: str, code: str, *, signer: Signer) -> str:
    """Spend a claim code and return the device token the browser keeps."""
    with store.session_scope() as s:
        row = s.get(AttestDevice, device_id, with_for_update=True)
        ok = (
            row is not None
            and row.retired_at is None
            and row.claim_hash is not None
            and row.claim_expires_at is not None
            and _now() <= row.claim_expires_at
            and hmac.compare_digest(row.claim_hash, _hash(code or ""))
        )
        if not ok:
            raise ValueError("the device claim code is wrong, used or expired")
        row.claim_hash = None
        row.claim_expires_at = None
        site_id = row.site_id
        s.commit()
    body = canonical_bytes(
        normalise({"device_id": device_id, "site_id": site_id, "issued_at": _now()})
    )
    sig = signer.sign(DEVICE_DOMAIN + body)
    return f"{_b64(body)}.{_b64(sig)}"


def read_device_token(token: str, public_keys: Mapping[str, bytes]) -> tuple[str, str]:
    """``(device_id, site_id)`` from a device token the node signed. Whether
    the device is still enrolled is a separate question: ask :func:`get`."""
    try:
        body_b64, sig_b64 = token.split(".")
        body, sig = _unb64(body_b64), _unb64(sig_b64)
        claims = json.loads(body)
    except (ValueError, TypeError, AttributeError):
        raise ValueError("the device token is malformed") from None
    for public in public_keys.values():
        try:
            if ed25519_verify(public, DEVICE_DOMAIN + body, sig):
                return claims["device_id"], claims["site_id"]
        except ValueError:
            continue
    raise ValueError("the device token's signature does not verify")


__all__ = [
    "CLAIM_TTL",
    "DEVICE_DOMAIN",
    "Enrolment",
    "MOBILITY",
    "enroll",
    "get",
    "issue_claim_code",
    "read_device_token",
    "redeem_claim",
    "retire",
]
