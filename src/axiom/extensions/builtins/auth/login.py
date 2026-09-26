# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0

"""Login orchestration (AUTH-6) — device-code sign-in end to end.

Runs the device flow, derives the user from the id_token, and stores the refresh
token via the vault-keyed custody. Dependency-injected (http/store/sleep/prompt)
so the whole path is testable against a fake IdP.
"""

from __future__ import annotations

from typing import Any, Callable, Optional

from axiom.extensions.builtins.auth.device_flow import poll_for_token, start_device_flow
from axiom.extensions.builtins.auth.flow import parse_id_token
from axiom.extensions.builtins.auth.providers import IdpConfig
from axiom.extensions.builtins.auth.token_store import store_refresh_token


def login_with_device_code(
    *,
    idp: IdpConfig,
    client_id: str,
    scopes: list,
    http: Any,
    store: Optional[Any] = None,
    user: Optional[str] = None,
    prompt: Optional[Callable[[str], None]] = None,
    sleep: Optional[Callable[[float], None]] = None,
) -> dict:
    """Device-code sign-in: prompt the user, poll, derive identity, store the
    refresh token. Returns ``{user, scopes, stored}``."""
    prompt = prompt or (lambda _msg: None)
    device = start_device_flow(http, idp, client_id=client_id, scopes=scopes)
    prompt(
        f"To sign in, visit {device.get('verification_uri')} and enter code: "
        f"{device.get('user_code')}"
    )
    tokens = poll_for_token(
        http, idp, client_id=client_id, device_code=device["device_code"],
        interval=device.get("interval", 5), sleep=sleep,
    )

    if user is None and tokens.get("id_token"):
        claims = parse_id_token(tokens["id_token"])
        user = claims.get("preferred_username") or claims.get("email") or claims.get("sub")
    user = user or "me"

    stored = False
    if tokens.get("refresh_token"):
        store_refresh_token(idp.name, user, scopes, tokens["refresh_token"], store=store)
        stored = True
    return {"user": user, "scopes": list(scopes), "stored": stored}


__all__ = ["login_with_device_code"]
