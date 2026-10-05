# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Signing grants and device claims at the gate (ADR-146).

Webgate knows who is signed in and on what device; attest decides whether
that is enough to sign. These two routes connect them:

- ``POST /gate/devices/claim`` ``{device_id, code}`` spends an enrolled
  device's one-time claim code and sets the device cookie, a node-signed token
  naming the device. Without it, a browser is a personal session.
- ``POST /gate/grants`` ``{presentation_id, console_id?, presence_code?}``
  mints a signing grant from the gate session and the device cookie. A
  refusal is 403 with a ``code``; ``reauth_required`` also returns the
  ``max_age`` to sign in again with (``/gate/oidc/login?max_age=…``).

The person is named the way every other surface names them,
``principal_from_idp_subject(sub, site)``, so a grant is for the same handle
a draft is for.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response

from axiom.webauth import session_from_cookies

DEVICE_COOKIE = "axiom_device"
_DEVICE_COOKIE_AGE = 365 * 24 * 3600


def register_signing_routes(
    router: APIRouter, *, issuer_of: Callable[[Request], str], secure: bool
) -> None:
    @router.post("/gate/devices/claim")
    async def claim_device(request: Request) -> Response:
        from axiom.extensions.builtins.attest import devices, signing

        body = await _json(request)
        try:
            token = devices.redeem_claim(
                str(body.get("device_id") or ""),
                str(body.get("code") or ""),
                signer=signing.signer(),
            )
        except ValueError as exc:
            return JSONResponse({"detail": str(exc), "code": "invalid_claim"}, status_code=403)
        resp = JSONResponse({"device_id": body["device_id"], "claimed": True})
        resp.set_cookie(
            DEVICE_COOKIE,
            token,
            max_age=_DEVICE_COOKIE_AGE,
            httponly=True,
            secure=secure,
            samesite="strict",
            path="/gate",
        )
        return resp

    @router.post("/gate/grants")
    async def mint_grant(request: Request) -> Response:
        from axiom.extensions.builtins.attest import devices, grants, signing
        from axiom.extensions.builtins.attest.service import AttestRefused
        from axiom.infra.principal import PrincipalContext, principal_from_idp_subject

        claims = session_from_cookies(request.cookies, issuer=issuer_of(request))
        if claims is None:
            return JSONResponse({"detail": "sign in first", "code": "no_session"}, status_code=401)
        try:
            handle = principal_from_idp_subject(
                str(claims.get("sub") or ""), str(claims.get("site") or "")
            )
        except ValueError:
            return JSONResponse(
                {"detail": "this sign-in cannot be named", "code": "no_session"}, status_code=401
            )
        idp = claims.get("idp")
        auth_time = claims.get("auth_time")
        auth = grants.Authentication(
            principal=PrincipalContext(
                handle=handle, posture="sso" if idp else "attested", assured=True, idp=idp
            ),
            auth_time=datetime.fromtimestamp(auth_time, UTC)
            if isinstance(auth_time, int)
            else None,
            amr=tuple(claims.get("amr") or ()),
        )
        device_id = f"session:{claims.get('sub')}"
        cookie = request.cookies.get(DEVICE_COOKIE)
        if cookie:
            try:
                device_id, _site = devices.read_device_token(cookie, signing.public_keys())
            except ValueError:
                pass  # an unreadable device cookie proves nothing: a personal session
        body = await _json(request)
        try:
            token = grants.mint(
                str(body.get("presentation_id") or ""),
                auth=auth,
                device=grants.Device(
                    device_id=device_id,
                    device_class="personal",
                    console_id=body.get("console_id"),
                ),
                presence_code=body.get("presence_code"),
                signer=signing.signer(),
            )
        except grants.GrantRefused as exc:
            payload: dict[str, Any] = {"detail": str(exc), "code": exc.code}
            if exc.max_age is not None:
                payload["max_age"] = exc.max_age
            return JSONResponse(payload, status_code=403)
        except AttestRefused as exc:
            return JSONResponse({"detail": str(exc), "code": "not_found"}, status_code=404)
        read = grants.read(token, signing.public_keys())
        return JSONResponse({"grant": token, "expires_at": read["expires_at"]})


async def _json(request: Request) -> dict[str, Any]:
    try:
        body = await request.json()
    except ValueError:
        return {}
    return body if isinstance(body, dict) else {}


__all__ = ["DEVICE_COOKIE", "register_signing_routes"]
