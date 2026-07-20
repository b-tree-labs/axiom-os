# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""OAuth 2.1 Authorization Server + OpenID Connect discovery metadata (ADR-082).

Axiom is a first-party AS/OIDC provider (the `oauth` builtin), distinct from the
`auth` relying-party (ADR-075). These builders produce the RFC 8414
(authorization-server) and OpenID Connect discovery documents a client fetches to
learn our endpoints and signing algorithm. Tokens are ES256, verified from the
JWKS (ADR-085); PKCE S256 is mandatory; audience binding (RFC 8707) is supported.

The `authorize` / `token` handlers land in the next build cut — the endpoint URLs
are advertised here as the committed contract so discovery is stable ahead of them.
"""

from __future__ import annotations

_SIGNING_ALGS = ["ES256"]


def authorization_server_metadata(issuer: str) -> dict:
    """RFC 8414 OAuth 2.0 Authorization Server Metadata."""
    issuer = issuer.rstrip("/")
    return {
        "issuer": issuer,
        "authorization_endpoint": f"{issuer}/oauth/authorize",
        "token_endpoint": f"{issuer}/oauth/token",
        "jwks_uri": f"{issuer}/.well-known/jwks.json",
        "registration_endpoint": f"{issuer}/oauth/register",
        "revocation_endpoint": f"{issuer}/oauth/revoke",
        "introspection_endpoint": f"{issuer}/oauth/introspect",
        "response_types_supported": ["code"],
        "grant_types_supported": [
            "authorization_code",
            "refresh_token",
            "client_credentials",
        ],
        "code_challenge_methods_supported": ["S256"],  # PKCE mandatory (OAuth 2.1)
        "token_endpoint_auth_methods_supported": [
            "none",  # public clients (PKCE)
            "client_secret_basic",
            "private_key_jwt",
        ],
        "scopes_supported": ["openid", "profile", "offline_access"],
        "id_token_signing_alg_values_supported": _SIGNING_ALGS,
        "token_endpoint_auth_signing_alg_values_supported": _SIGNING_ALGS,
        # RFC 8707 — a client may request an audience-restricted token.
        "resource_indicators_supported": True,
    }


def openid_configuration(issuer: str) -> dict:
    """OpenID Connect Discovery 1.0 document (extends the AS metadata)."""
    md = authorization_server_metadata(issuer)
    issuer = issuer.rstrip("/")
    md.update(
        {
            "userinfo_endpoint": f"{issuer}/oauth/userinfo",
            "subject_types_supported": ["public"],
            "claims_supported": [
                "sub",
                "iss",
                "aud",
                "exp",
                "iat",
                "auth_time",
                "acr",
                "amr",
                "email",
                "name",
            ],
        }
    )
    return md


__all__ = ["authorization_server_metadata", "openid_configuration"]
