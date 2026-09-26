# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0

"""OIDC identity-provider configs (AUTH-R5). Endpoints come from a per-provider
helper or OIDC discovery (``.well-known/openid-configuration``). Entra is
tenant-scoped (e.g. an institutional Entra tenant); Google + generic are issuer-scoped."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class IdpConfig:
    name: str
    authorization_endpoint: str
    token_endpoint: str
    issuer: str | None = None
    jwks_uri: str | None = None
    device_authorization_endpoint: str | None = None
    default_scopes: tuple = field(default_factory=tuple)


def entra(tenant_id: str) -> IdpConfig:
    """Microsoft Entra ID (Azure AD) v2.0 — a tenant-scoped IdP."""
    base = f"https://login.microsoftonline.com/{tenant_id}"
    return IdpConfig(
        name="entra",
        authorization_endpoint=f"{base}/oauth2/v2.0/authorize",
        token_endpoint=f"{base}/oauth2/v2.0/token",
        issuer=f"https://login.microsoftonline.com/{tenant_id}/v2.0",
        jwks_uri=f"{base}/discovery/v2.0/keys",
        device_authorization_endpoint=f"{base}/oauth2/v2.0/devicecode",
        default_scopes=("openid", "profile", "email", "offline_access"),
    )


def google() -> IdpConfig:
    return IdpConfig(
        name="google",
        authorization_endpoint="https://accounts.google.com/o/oauth2/v2/auth",
        token_endpoint="https://oauth2.googleapis.com/token",
        issuer="https://accounts.google.com",
        jwks_uri="https://www.googleapis.com/oauth2/v3/certs",
        device_authorization_endpoint="https://oauth2.googleapis.com/device/code",
        default_scopes=("openid", "email", "profile"),
    )


def from_discovery(http: Any, issuer: str) -> IdpConfig:
    """Build a config from any OIDC issuer's discovery document."""
    doc = http.get(issuer.rstrip("/") + "/.well-known/openid-configuration")
    return IdpConfig(
        name=doc.get("issuer", issuer),
        authorization_endpoint=doc["authorization_endpoint"],
        token_endpoint=doc["token_endpoint"],
        issuer=doc.get("issuer", issuer),
        jwks_uri=doc.get("jwks_uri"),
        default_scopes=("openid", "email", "profile"),
    )


# --- Open IdP registry (ADR-075 §2) --------------------------------------
# The same shape as directory/calendar/secrets: a name -> builder map, an open
# `register_idp` so a new provider (Okta, Cognito, any OIDC issuer) is added
# without editing a single caller, and a `get_idp` that raises loudly on an
# unknown name. Builders take **config so selection is uniform; each pulls the
# keys it needs. This replaces the closed if/elif dispatch that had callers
# default an unknown provider to Entra.


def _entra_from_config(**config: Any) -> IdpConfig:
    tenant = config.get("tenant") or config.get("tenant_id")
    if not tenant:
        raise ValueError("entra idp requires a 'tenant' (tenant id)")
    return entra(tenant)


def _google_from_config(**config: Any) -> IdpConfig:
    return google()


_IDP_REGISTRY: dict[str, Any] = {
    "entra": _entra_from_config,
    "google": _google_from_config,
}


def register_idp(name: str, builder: Any) -> None:
    """Add an IdP (``okta``, ``cognito``, a generic OIDC issuer…) without
    touching callers. ``builder(**config) -> IdpConfig``."""
    _IDP_REGISTRY[name] = builder


def available_idps() -> tuple[str, ...]:
    return tuple(sorted(_IDP_REGISTRY))


def get_idp(name: str, **config: Any) -> IdpConfig:
    try:
        builder = _IDP_REGISTRY[name]
    except KeyError:
        raise ValueError(
            f"unknown idp {name!r}; available: {', '.join(available_idps())}"
        ) from None
    return builder(**config)


__all__ = [
    "IdpConfig",
    "available_idps",
    "entra",
    "from_discovery",
    "get_idp",
    "google",
    "register_idp",
]
