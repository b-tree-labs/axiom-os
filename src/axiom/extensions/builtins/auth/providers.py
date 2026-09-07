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


__all__ = ["IdpConfig", "entra", "from_discovery", "google"]
