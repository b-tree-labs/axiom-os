# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0

"""Box Client Credentials Grant (CCG) auth — server-to-server, no keypair.

The simplest unattended Box auth: a Client ID + Client Secret + Enterprise
ID exchange for an auto-refreshing access token. No private key to generate,
download, or store — just three short strings (the "paste a key, done"
onboarding path). With the app granted "App + Enterprise Access" and
admin-authorized, the token reads all enterprise content with no per-folder
collaboration.

Interface mirrors :class:`BoxJwtAuth` (``authorization_header`` /
``get_access_token``) so :class:`BoxSessionApiClient` consumes either.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass

import requests

_TOKEN_URL = "https://api.box.com/oauth2/token"


@dataclass(frozen=True)
class BoxCcgConfig:
    """Box CCG app credentials (the three values from the dev console)."""

    client_id: str
    client_secret: str
    enterprise_id: str

    @classmethod
    def from_dict(cls, blob: dict) -> BoxCcgConfig:
        try:
            return cls(
                client_id=blob["client_id"],
                client_secret=blob["client_secret"],
                enterprise_id=str(blob["enterprise_id"]),
            )
        except (KeyError, TypeError) as exc:
            raise ValueError(
                f"BoxCcgConfig: missing field in config — got {exc!r}; "
                "expected client_id, client_secret, enterprise_id."
            ) from exc

    @staticmethod
    def is_ccg_blob(blob: dict) -> bool:
        """True when the blob is CCG-shaped (vs the JWT keypair JSON)."""
        return (
            isinstance(blob, dict)
            and "boxAppSettings" not in blob
            and "client_id" in blob
            and "enterprise_id" in blob
        )


class BoxCcgAuth:
    """Mints + caches Box access tokens via the client_credentials grant."""

    def __init__(self, cfg: BoxCcgConfig, *, refresh_window_s: int = 300) -> None:
        self._cfg = cfg
        self._refresh_window_s = refresh_window_s
        self._lock = threading.Lock()
        self._cached: tuple[str, float] | None = None  # (token, expires_at)

    def get_access_token(self) -> str:
        with self._lock:
            now = time.time()
            if self._cached is not None:
                token, expires_at = self._cached
                if expires_at - now > self._refresh_window_s:
                    return token
            token, expires_at = self._mint()
            self._cached = (token, expires_at)
            return token

    def authorization_header(self) -> str:
        return f"Bearer {self.get_access_token()}"

    def _mint(self) -> tuple[str, float]:
        resp = requests.post(
            _TOKEN_URL,
            data={
                "grant_type": "client_credentials",
                "client_id": self._cfg.client_id,
                "client_secret": self._cfg.client_secret,
                "box_subject_type": "enterprise",
                "box_subject_id": self._cfg.enterprise_id,
            },
            timeout=30,
        )
        if not resp.ok:
            raise RuntimeError(
                f"Box CCG token exchange failed: {resp.status_code} "
                f"{resp.text[:200]}"
            )
        body = resp.json()
        # Box access tokens default to 3600s; keep headroom for drift.
        expires_at = time.time() + int(body.get("expires_in", 3600)) - 300
        return (body["access_token"], expires_at)


__all__ = ["BoxCcgConfig", "BoxCcgAuth"]
