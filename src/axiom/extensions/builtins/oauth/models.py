# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Value types for the oauth AS (ADR-082).

A registered OAuth client — the confidential machine identity behind the
``client_credentials`` grant, or the public app behind ``authorization_code``.
Kept a frozen dataclass so a client record is immutable once loaded; the
registry (:mod:`.clients`) owns lookup, this owns shape.
"""

from __future__ import annotations

from dataclasses import dataclass, field

#: Client authentication methods (RFC 6749 §2.3, RFC 7523). ``none`` is a public
#: client that proves possession via PKCE instead of a secret.
CLIENT_SECRET_BASIC = "client_secret_basic"
PRIVATE_KEY_JWT = "private_key_jwt"
AUTH_METHOD_NONE = "none"


@dataclass(frozen=True)
class OAuthClient:
    """A registered OAuth 2.1 client.

    ``client_secret_hash`` is a scrypt hash (``axiom.webauth.get_password_hash``),
    never the plaintext secret; ``None`` marks a public client. ``scopes`` and
    ``audiences`` are the ceilings this client may request — a token request may
    narrow within them but never exceed them (invalid_scope / invalid_target).
    """

    client_id: str
    client_secret_hash: str | None = None
    grant_types: tuple[str, ...] = ("client_credentials",)
    scopes: tuple[str, ...] = ()
    audiences: tuple[str, ...] = ()
    redirect_uris: tuple[str, ...] = ()
    token_endpoint_auth_method: str = CLIENT_SECRET_BASIC
    name: str = ""
    metadata: dict = field(default_factory=dict)

    @property
    def is_public(self) -> bool:
        """A public client holds no secret and authenticates via PKCE."""
        return self.client_secret_hash is None


__all__ = [
    "AUTH_METHOD_NONE",
    "CLIENT_SECRET_BASIC",
    "PRIVATE_KEY_JWT",
    "OAuthClient",
]
