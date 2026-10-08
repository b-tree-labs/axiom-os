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
    """Build a config from any OIDC issuer's discovery document.

    Every endpoint :class:`IdpConfig` has somewhere to put is copied, including
    ``device_authorization_endpoint``. That one used to be dropped, and a
    dropped field is worse than a missing one: ``start_device_flow`` then
    raised "<name> has no device authorization endpoint", which accused the
    identity provider of an omission that was ours. Until this, every issuer
    reached by discovery rather than by one of the two hand-written presets was
    silently without a device flow — and the device flow is how somebody with
    no browser session gets a credential.

    ``name`` and ``default_scopes`` are ours rather than the issuer's. The
    document is read for the fields we declare and never for attributes we do
    not, so an issuer cannot shape our config.
    """
    doc = http.get(issuer.rstrip("/") + "/.well-known/openid-configuration")
    return IdpConfig(
        name=doc.get("issuer", issuer),
        authorization_endpoint=doc["authorization_endpoint"],
        token_endpoint=doc["token_endpoint"],
        issuer=doc.get("issuer", issuer),
        jwks_uri=doc.get("jwks_uri"),
        device_authorization_endpoint=doc.get("device_authorization_endpoint"),
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


def _oidc_from_config(**config: Any) -> IdpConfig:
    """Any OIDC issuer, by discovery — the generic entry.

    Without this the open registry was closed in practice. It shipped two
    presets, so any other issuer needed a caller to import ``from_discovery``
    itself, which is the hand-rolled dispatch the registry replaced. An
    institutional single sign-on, Okta, Cognito, Keycloak and the fake issuer
    the test suite runs against are all this entry plus an issuer URL.

    Both keys are named in the refusal. A factory that raises an
    unknown-shaped ``TypeError`` from inside is how somebody concludes the
    registry does not support their issuer.
    """
    issuer = config.get("issuer")
    http = config.get("http")
    if not issuer:
        raise ValueError("oidc idp requires an 'issuer' (e.g. https://id.example.edu)")
    if http is None:
        raise ValueError("oidc idp requires an 'http' client to fetch the discovery document")
    return from_discovery(http, issuer)


_IDP_REGISTRY: dict[str, Any] = {
    "entra": _entra_from_config,
    "google": _google_from_config,
    "oidc": _oidc_from_config,
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
