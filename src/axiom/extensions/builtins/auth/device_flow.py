# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0

"""OAuth 2.0 Device Authorization Grant (AUTH-3, RFC 8628, AUTH-R9).

For headless / no-browser / locked-down hosts (e.g. an institution's network server): the user
visits a URL on any device and enters a short code; the node polls for the
tokens. Built over the injected HTTP client, so it's testable against a fake IdP.
"""

from __future__ import annotations

import time
from typing import Any, Callable, Optional

from axiom.extensions.builtins.auth.providers import IdpConfig

_DEVICE_GRANT = "urn:ietf:params:oauth:grant-type:device_code"


class DeviceFlowError(Exception):
    """The device authorization flow failed or expired."""


def start_device_flow(http: Any, idp: IdpConfig, *, client_id: str, scopes: list) -> dict:
    """Begin the flow. Returns the IdP's response (device_code, user_code,
    verification_uri, interval, expires_in) — show user_code + verification_uri
    to the user."""
    if not idp.device_authorization_endpoint:
        raise DeviceFlowError(f"{idp.name} has no device authorization endpoint")
    return http.post(
        idp.device_authorization_endpoint,
        {"client_id": client_id, "scope": " ".join(scopes)},
    )


def poll_for_token(
    http: Any,
    idp: IdpConfig,
    *,
    client_id: str,
    device_code: str,
    interval: int = 5,
    max_polls: int = 60,
    sleep: Optional[Callable[[float], None]] = None,
) -> dict:
    """Poll the token endpoint until the user approves. Honors
    ``authorization_pending`` (keep waiting) and ``slow_down`` (back off)."""
    sleep = sleep or time.sleep
    for _ in range(max_polls):
        resp = http.post(idp.token_endpoint, {
            "grant_type": _DEVICE_GRANT, "device_code": device_code, "client_id": client_id,
        })
        if "access_token" in resp:
            return resp
        error = resp.get("error")
        if error == "authorization_pending":
            sleep(interval)
            continue
        if error == "slow_down":
            interval += 5
            sleep(interval)
            continue
        raise DeviceFlowError(f"device authorization failed: {error or 'unknown'}")
    raise DeviceFlowError("device authorization timed out")


__all__ = ["DeviceFlowError", "poll_for_token", "start_device_flow"]
