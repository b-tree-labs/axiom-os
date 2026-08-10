# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0

"""Per-vendor inbound signature verification (ADR-067 PR-1).

Mirrors the verified-receiver pattern in ``release/webhook_receiver.py``:
each vendor is a ``WebhookVerifier`` (verify raw body + headers) registered
under its name. The scaffold ships the Protocol, a registry, and an
``HmacSha256Verifier`` base; the concrete Slack signing-secret verifier
and Twilio X-Twilio-Signature verifier land with their inbound PRs.
"""

from __future__ import annotations

import hashlib
import hmac
from collections.abc import Mapping
from typing import Protocol, runtime_checkable


@runtime_checkable
class WebhookVerifier(Protocol):
    def verify(self, *, headers: Mapping[str, str], body: bytes) -> bool: ...


class AllowAllVerifier:
    """Dev/test default — accepts everything.

    NOT for production: a vendor with no real verifier registered must be
    rejected by the gateway (404), never silently allowed. This exists so
    the scaffold + tests have a known-good verifier to exercise the route.
    """

    def verify(self, *, headers: Mapping[str, str], body: bytes) -> bool:
        return True


class HmacSha256Verifier:
    """Generic HMAC-SHA256 over the raw body, hex-compared in constant time.

    Concrete vendors subclass/configure the header name + any prefix
    (Slack prepends ``v0=`` over ``v0:timestamp:body``; this base covers
    the plain-body case and is the building block for PR-4).
    """

    def __init__(self, secret: str, *, header: str) -> None:
        self._secret = secret.encode("utf-8")
        self._header = header

    def verify(self, *, headers: Mapping[str, str], body: bytes) -> bool:
        provided = headers.get(self._header, "")
        if not provided:
            return False
        expected = hmac.new(self._secret, body, hashlib.sha256).hexdigest()
        return hmac.compare_digest(expected, provided)


class VerifierRegistry:
    def __init__(self) -> None:
        self._by_vendor: dict[str, WebhookVerifier] = {}

    def register(self, vendor: str, verifier: WebhookVerifier) -> None:
        self._by_vendor[vendor] = verifier

    def get(self, vendor: str) -> WebhookVerifier | None:
        return self._by_vendor.get(vendor)


__all__ = [
    "WebhookVerifier",
    "AllowAllVerifier",
    "HmacSha256Verifier",
    "VerifierRegistry",
]
