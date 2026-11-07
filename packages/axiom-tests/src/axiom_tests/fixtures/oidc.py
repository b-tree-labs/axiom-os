# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""``mock_oidc`` fixture — fake OIDC IdP issuing predictable JWT-ish tokens.

The tokens are not cryptographically signed; they use a deterministic,
reversible encoding so tests can decode, assert, and round-trip claims
without pulling in a real crypto library. This is deliberately a test
double — never use this encoding in production.
"""

from __future__ import annotations

import base64
import json
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

import pytest


@dataclass
class OIDCToken:
    """A fake OIDC token (not cryptographically signed)."""

    subject: str
    issuer: str
    audience: str
    claims: dict[str, Any] = field(default_factory=dict)
    issued_at: int = field(default_factory=lambda: int(time.time()))
    expires_at: int = 0

    def encode(self) -> str:
        """Return a fake ``header.payload.signature`` string."""
        header = {"alg": "none", "typ": "JWT"}
        payload = {
            "iss": self.issuer,
            "sub": self.subject,
            "aud": self.audience,
            "iat": self.issued_at,
            "exp": self.expires_at,
            **self.claims,
        }
        return ".".join(
            [
                _b64url_encode(json.dumps(header, sort_keys=True).encode()),
                _b64url_encode(json.dumps(payload, sort_keys=True).encode()),
                "fake-signature",
            ]
        )


class MockOIDC:
    """Fake OIDC IdP — issues and validates tokens deterministically."""

    def __init__(
        self,
        *,
        issuer: str = "https://oidc.test.example.org",
        default_audience: str = "axiom",
        default_ttl_seconds: int = 3600,
    ) -> None:
        self.issuer = issuer
        self.default_audience = default_audience
        self.default_ttl_seconds = default_ttl_seconds
        self.issued: list[OIDCToken] = []

    def issue(
        self,
        *,
        subject: str,
        audience: str | None = None,
        claims: dict[str, Any] | None = None,
        ttl_seconds: int | None = None,
    ) -> OIDCToken:
        now = int(time.time())
        ttl = ttl_seconds if ttl_seconds is not None else self.default_ttl_seconds
        merged_claims = {"jti": str(uuid.uuid4())}
        if claims:
            merged_claims.update(claims)
        token = OIDCToken(
            subject=subject,
            issuer=self.issuer,
            audience=audience or self.default_audience,
            claims=merged_claims,
            issued_at=now,
            expires_at=now + ttl,
        )
        self.issued.append(token)
        return token

    def decode(self, encoded: str) -> dict[str, Any]:
        """Decode a token emitted by :meth:`OIDCToken.encode`."""
        parts = encoded.split(".")
        if len(parts) != 3:
            raise ValueError("not a fake-JWT-shaped string")
        _, payload_b64, _ = parts
        return json.loads(_b64url_decode(payload_b64))

    def is_valid(self, encoded: str, *, audience: str | None = None) -> bool:
        try:
            payload = self.decode(encoded)
        except (ValueError, json.JSONDecodeError):
            return False
        if payload.get("iss") != self.issuer:
            return False
        if audience is not None and payload.get("aud") != audience:
            return False
        return payload.get("exp", 0) >= int(time.time())


def _b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64url_decode(s: str) -> bytes:
    padding = "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(s + padding)


@pytest.fixture
def mock_oidc() -> MockOIDC:
    """Provide a fresh ``MockOIDC`` for each test."""
    return MockOIDC()


__all__ = ["MockOIDC", "OIDCToken", "mock_oidc"]
