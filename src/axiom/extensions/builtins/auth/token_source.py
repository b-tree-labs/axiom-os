# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0

"""The token source (AUTH-R4) — a callable that always yields a non-expired
access token, refreshing silently from a refresh token. This is exactly what a
connector passes to a provider's ``token_source`` config; the connector never
touches OAuth. Refresh-token rotation is honored."""

from __future__ import annotations

import time
from typing import Any, Callable, Optional

from axiom.extensions.builtins.auth import flow
from axiom.extensions.builtins.auth.providers import IdpConfig


class TokenSource:
    """Caches an access token; refreshes within ``skew`` seconds of expiry."""

    def __init__(
        self,
        http: Any,
        idp: IdpConfig,
        *,
        client_id: str,
        refresh_token: str,
        client_secret: Optional[str] = None,
        scopes: Optional[list] = None,
        now: Optional[Callable[[], float]] = None,
        skew_seconds: int = 60,
    ) -> None:
        self._http = http
        self._idp = idp
        self._client_id = client_id
        self._client_secret = client_secret
        self._refresh_token = refresh_token
        self._scopes = scopes
        self._now = now or time.time
        self._skew = skew_seconds
        self._access: Optional[str] = None
        self._expires_at: float = 0.0

    def __call__(self) -> str:
        if self._access is None or self._now() >= self._expires_at - self._skew:
            self._renew()
        assert self._access is not None
        return self._access

    def _renew(self) -> None:
        tok = flow.refresh(
            self._http, self._idp,
            client_id=self._client_id, refresh_token=self._refresh_token,
            client_secret=self._client_secret, scopes=self._scopes,
        )
        self._access = tok["access_token"]
        self._expires_at = self._now() + int(tok.get("expires_in", 3600))
        if tok.get("refresh_token"):           # rotation
            self._refresh_token = tok["refresh_token"]


__all__ = ["TokenSource"]
