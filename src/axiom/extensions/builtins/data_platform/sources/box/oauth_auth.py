# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0

"""Box OAuth 2.0 refresh-token auth — durable user-delegated access.

The no-enterprise-admin path: a standard OAuth app + a one-time browser
login yields a refresh token that mints fresh 60-minute access tokens for
up to 60 days, acting AS the authorizing user. Unlike a Developer Token
(60 min, no refresh) this runs unattended for weeks; unlike JWT/CCG it
needs no enterprise-admin authorization.

Critical Box behavior: **refresh tokens are single-use and ROTATE** — every
refresh returns a *new* refresh token (and invalidates the old). So the
current refresh token must be persisted to disk and re-read on each mint,
or the chain breaks. This class owns that persistence (atomic write).

Interface mirrors :class:`BoxJwtAuth` / :class:`BoxCcgAuth`
(``authorization_header`` / ``get_access_token``) so
:class:`BoxSessionApiClient` consumes any of them.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path

import requests

_TOKEN_URL = "https://api.box.com/oauth2/token"


@dataclass(frozen=True)
class BoxOAuthConfig:
    """OAuth app creds + where the rotating refresh token is persisted."""

    client_id: str
    client_secret: str
    # File holding the *current* refresh token (rotated on each use). Seeded
    # by the one-time login flow; updated by this auth on every refresh.
    token_store: Path

    @classmethod
    def from_dict(cls, blob: dict) -> BoxOAuthConfig:
        try:
            store = blob.get("token_store") or os.path.expanduser(
                "~/.axi/credentials/box/oauth_refresh.json"
            )
            cfg = cls(
                client_id=blob["client_id"],
                client_secret=blob["client_secret"],
                token_store=Path(store),
            )
        except (KeyError, TypeError) as exc:
            raise ValueError(
                f"BoxOAuthConfig: missing field — got {exc!r}; expected "
                "client_id, client_secret (+ optional token_store)."
            ) from exc
        # Seed the store from an inline refresh_token if provided and the
        # store doesn't exist yet (first run after login capture).
        if blob.get("refresh_token") and not cfg.token_store.exists():
            cfg.token_store.parent.mkdir(parents=True, exist_ok=True)
            _atomic_write(cfg.token_store, {"refresh_token": blob["refresh_token"]})
        return cfg

    @staticmethod
    def is_oauth_blob(blob: dict) -> bool:
        """True when the blob is OAuth-shaped (refresh-token flow)."""
        return (
            isinstance(blob, dict)
            and "boxAppSettings" not in blob          # not JWT
            and "client_id" in blob
            and "refresh_token" in blob               # the OAuth discriminator
        )


def _atomic_write(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent))
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f)
        os.replace(tmp, path)
        os.chmod(path, 0o600)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


class BoxOAuthAuth:
    """Mints + caches access tokens from a rotating OAuth refresh token."""

    def __init__(self, cfg: BoxOAuthConfig, *, refresh_window_s: int = 300) -> None:
        self._cfg = cfg
        self._refresh_window_s = refresh_window_s
        self._lock = threading.Lock()
        self._cached: tuple[str, float] | None = None  # (access_token, expires_at)

    def invalidate(self) -> None:
        """Drop the cached access token so the next mint forces a refresh.
        Called on a 401 to recover from a revoked / early-expired token."""
        with self._lock:
            self._cached = None

    def get_access_token(self) -> str:
        with self._lock:
            now = time.time()
            if self._cached is None:
                # Reuse a still-valid access token persisted by another
                # process/instance. Without this, every freshly-constructed
                # BoxOAuthAuth (e.g. the Dagster sensor rebuilds the source
                # each 60s tick) would refresh — rotating the refresh token
                # every minute and racing concurrent runs into an
                # invalid_grant desync. Sharing the cached access token via
                # token_store collapses that to ~hourly refreshes.
                self._cached = self._load_cached_access()
            if self._cached is not None:
                tok, exp = self._cached
                if exp - now > self._refresh_window_s:
                    return tok
            tok, exp = self._refresh()
            self._cached = (tok, exp)
            return tok

    def _load_cached_access(self) -> tuple[str, float] | None:
        """Load a persisted (access_token, expires_at) from token_store, if any."""
        try:
            blob = json.loads(self._cfg.token_store.read_text())
        except (FileNotFoundError, json.JSONDecodeError):
            return None
        tok, exp = blob.get("access_token"), blob.get("expires_at")
        if tok and isinstance(exp, (int, float)):
            return (tok, float(exp))
        return None

    def authorization_header(self) -> str:
        return f"Bearer {self.get_access_token()}"

    def _read_refresh(self) -> str:
        try:
            return json.loads(self._cfg.token_store.read_text())["refresh_token"]
        except (FileNotFoundError, KeyError, json.JSONDecodeError) as exc:
            raise RuntimeError(
                f"BoxOAuthAuth: no refresh token at {self._cfg.token_store}. "
                "Run the one-time login (scripts/box_oauth_login.py) first."
            ) from exc

    def _refresh(self) -> tuple[str, float]:
        rt = self._read_refresh()
        resp = requests.post(
            _TOKEN_URL,
            data={
                "grant_type": "refresh_token",
                "refresh_token": rt,
                "client_id": self._cfg.client_id,
                "client_secret": self._cfg.client_secret,
            },
            timeout=30,
        )
        if not resp.ok:
            raise RuntimeError(
                f"Box OAuth refresh failed: {resp.status_code} {resp.text[:200]}. "
                "The refresh token may have expired (60d) or been rotated out of "
                "sync — re-run the login flow."
            )
        body = resp.json()
        # Box rotates the refresh token on every use — PERSIST the new one
        # immediately or the next refresh will fail.
        new_rt = body.get("refresh_token") or rt
        expires_at = time.time() + int(body.get("expires_in", 3600)) - 300
        # Persist BOTH the rotated refresh token AND the access token (+expiry)
        # so other instances reuse the access token instead of re-refreshing.
        _atomic_write(self._cfg.token_store, {
            "refresh_token": new_rt,
            "access_token": body["access_token"],
            "expires_at": expires_at,
        })
        return (body["access_token"], expires_at)


__all__ = ["BoxOAuthConfig", "BoxOAuthAuth"]
