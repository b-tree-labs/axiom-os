# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0

"""Connect a calendar using the new auth fabric.

The bridge between the `auth` extension and the calendar providers: declare the
calendar's credential need (a `consumes`-credential), resolve it through
``auth.consumes.resolve_credential`` (which enforces the posture floor and yields
a refreshing ``token_source``), and build the provider with it. So after
`axi auth login`, the calendar acts as the **user** (delegated), posture-gated —
no service-account, no OAuth code in the calendar layer.
"""

from __future__ import annotations

from typing import Any, Callable, Optional

# Default IdP per calendar vendor (an institution's IdP may be Entra, same as M365).
_VENDOR_IDP = {"m365": "entra", "google": "google"}
# Default delegated scopes per vendor.
_VENDOR_SCOPES = {
    "m365": ["https://graph.microsoft.com/Calendars.ReadWrite", "offline_access"],
    "google": ["https://www.googleapis.com/auth/calendar", "openid", "email"],
}


def connect_calendar(
    vendor: str,
    *,
    principal: Any,
    http: Any,
    client_id: str,
    user_id: Optional[str] = None,
    scopes: Optional[list] = None,
    idp: Optional[str] = None,
    tenant: Optional[str] = None,
    calendar_id: Optional[str] = None,
    client_secret: Optional[str] = None,
    store: Optional[Any] = None,
    min_posture: str = "open",
    now: Optional[Callable[[], float]] = None,
):
    """Build a calendar provider authed via a delegated `token_source`.

    Raises ``auth.consumes.PostureError`` if ``principal`` is below ``min_posture``.
    """
    from axiom.extensions.builtins.auth.consumes import resolve_credential
    from axiom.extensions.builtins.schedule.calendar import get_provider

    scopes = scopes or _VENDOR_SCOPES.get(vendor, ["openid"])
    decl = {
        "kind": "credential",
        "idp": idp or _VENDOR_IDP.get(vendor, "entra"),
        "scopes": scopes,
        "min_posture": min_posture,
        "tenant": tenant,
    }
    token_source = resolve_credential(
        decl, principal=principal, http=http, client_id=client_id,
        client_secret=client_secret, store=store, tenant=tenant,
        user=user_id or getattr(principal, "handle", None), now=now,
    )

    config: dict = {"token_source": token_source}
    if calendar_id:
        config["calendar_id"] = calendar_id
    if user_id:
        config["user_id"] = user_id
    return get_provider(vendor, config)


def _idp_config(vendor: str, idp_name: str, tenant: Optional[str]):
    from axiom.extensions.builtins.auth.providers import entra, google

    if idp_name == "entra":
        if not tenant:
            raise ValueError("entra requires a tenant id")
        return entra(tenant)
    if idp_name == "google":
        return google()
    raise ValueError(f"unknown idp {idp_name!r}")


def connect_and_verify(
    vendor: str,
    *,
    idp_http: Any,
    client_id: str,
    tenant: Optional[str] = None,
    user: Optional[str] = None,
    scopes: Optional[list] = None,
    store: Optional[Any] = None,
    calendar_id: str = "primary",
    provider: Optional[Any] = None,
    min_posture: str = "open",
    prompt: Optional[Callable[[str], None]] = None,
    sleep: Optional[Callable[[float], None]] = None,
    now: Optional[Callable[[], float]] = None,
) -> dict:
    """The `axi calendar connect` wizard: sign in (device-code) if needed, build
    the provider authed as the user, and verify with a live round-trip."""
    from axiom.extensions.builtins.auth.login import login_with_device_code
    from axiom.extensions.builtins.auth.token_store import load_refresh_token
    from axiom.extensions.builtins.schedule.calendar.setup import _roundtrip
    from axiom.infra.principal import PrincipalContext

    scopes = scopes or _VENDOR_SCOPES.get(vendor, ["openid"])
    idp_name = _VENDOR_IDP.get(vendor, "entra")
    idp = _idp_config(vendor, idp_name, tenant)

    if user is None or load_refresh_token(idp_name, user, scopes, store=store) is None:
        result = login_with_device_code(
            idp=idp, client_id=client_id, scopes=scopes, http=idp_http,
            store=store, user=user, prompt=prompt, sleep=sleep)
        user = result["user"]

    principal = PrincipalContext(user, "sso", assured=True, idp=idp_name)
    if provider is None:
        provider = connect_calendar(
            vendor, principal=principal, http=idp_http, client_id=client_id,
            tenant=tenant, user_id=user, store=store, scopes=scopes,
            calendar_id=calendar_id, min_posture=min_posture, now=now)

    rt = _roundtrip(provider, calendar_id)
    return {"vendor": vendor, "user": user, "calendar_id": calendar_id,
            "verified": rt["ok"], "next_fires": rt["next_fires"]}


__all__ = ["connect_and_verify", "connect_calendar"]
