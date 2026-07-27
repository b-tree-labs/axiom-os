# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Box JWT (Server Authentication) auth provider.

Ends the 60-min dev-token cliff that's eaten through ~5 manual tokens
in tonight's stand-up. Reads a Box JWT app keypair config (downloaded
from the Box developer console after registering a "Server
Authentication (JWT)" app), mints fresh 60-min access tokens via the
box-sdk-python JWTAuth flow, caches them, and auto-refreshes before
expiry. Daemon never asks an operator for a token again.

Config sources (in priority order):

- ``BoxJwtConfig.from_json_path(path)`` — file path on disk (k8s
  Secret-mounted, OpenBao-templated, etc.)
- ``BoxJwtConfig.from_env(var_name)`` — env var holding the JSON blob

Usage::

    cfg = BoxJwtConfig.from_env("BOX_JWT_CONFIG")
    auth = BoxJwtAuth(cfg)
    client = BoxSessionApiClient(session_dir=p, jwt_auth=auth)
    # Each get_json / get_bytes call uses a freshly-minted access token.

The provider depends on ``boxsdk`` (PyPI: ``boxsdk[jwt]``). It's a
runtime dep listed in ``[data-platform]`` extras; not required for the
session-only path.
"""

from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Tuple


@dataclass(frozen=True)
class BoxJwtConfig:
    """Parsed Box JWT app config.

    Mirrors the shape Box's developer console gives you when you click
    "Download as JSON" on a Server Authentication app's keypair.
    """

    client_id: str
    client_secret: str
    public_key_id: str
    private_key: str
    passphrase: str
    enterprise_id: str

    @classmethod
    def from_dict(cls, blob: dict) -> "BoxJwtConfig":
        try:
            app = blob["boxAppSettings"]
            auth = app["appAuth"]
            return cls(
                client_id=app["clientID"],
                client_secret=app["clientSecret"],
                public_key_id=auth["publicKeyID"],
                private_key=auth["privateKey"],
                passphrase=auth["passphrase"],
                enterprise_id=blob["enterpriseID"],
            )
        except (KeyError, TypeError) as exc:
            raise ValueError(
                f"BoxJwtConfig: missing field in JSON blob — got {exc!r}; "
                "expected the schema produced by the Box developer console's "
                "'Download as JSON' button on the JWT app's keypair."
            ) from exc

    @classmethod
    def from_json_path(cls, path: Path) -> "BoxJwtConfig":
        return cls.from_dict(json.loads(Path(path).read_text()))

    @classmethod
    def from_env(cls, var: str) -> "BoxJwtConfig":
        raw = os.environ.get(var)
        if not raw:
            raise RuntimeError(
                f"BoxJwtConfig: env var {var!r} not set; "
                "expected the JSON config from the Box developer console."
            )
        return cls.from_dict(json.loads(raw))


class BoxJwtAuth:
    """Mints + caches Box access tokens via JWT server auth.

    Thread-safe — multiple parallel fetchers can call
    ``authorization_header()`` concurrently; tokens are cached until
    ``refresh_window_s`` before expiry.

    The expensive call (JWT sign + Box token-exchange) only fires when
    the cached token is missing or about to expire.
    """

    def __init__(self, cfg: BoxJwtConfig, *, refresh_window_s: int = 300) -> None:
        self._cfg = cfg
        self._refresh_window_s = refresh_window_s
        self._lock = threading.Lock()
        self._cached: Tuple[str, float] | None = None  # (token, expires_at)

    def get_access_token(self) -> str:
        """Return a fresh access token, minting/refreshing as needed."""
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
        """Return ``Bearer <token>`` ready to drop into request headers."""
        return f"Bearer {self.get_access_token()}"

    def _mint(self) -> Tuple[str, float]:
        """Sign a JWT + exchange it with Box for an access token.

        Returns ``(access_token, expires_at_unix)``. Raises ``RuntimeError``
        with the Box error if the exchange fails.
        """
        try:
            from boxsdk import JWTAuth
        except ImportError as exc:  # pragma: no cover - env guard
            raise RuntimeError(
                "boxsdk not installed. Install with: pip install 'boxsdk[jwt]'"
            ) from exc

        # boxsdk wants raw private key bytes, not the PEM string when
        # passphrase-encrypted; both shapes are accepted via the
        # `rsa_private_key_data` kwarg.
        auth = JWTAuth(
            client_id=self._cfg.client_id,
            client_secret=self._cfg.client_secret,
            jwt_key_id=self._cfg.public_key_id,
            rsa_private_key_data=self._cfg.private_key,
            rsa_private_key_passphrase=self._cfg.passphrase.encode(),
            enterprise_id=self._cfg.enterprise_id,
        )
        # Authenticate as the service account (enterprise app user).
        access_token = auth.authenticate_instance()
        # Box JWT tokens default to 60 min (3600s); we conservatively
        # treat them as 55 min to leave headroom for clock drift +
        # network jitter on the refresh.
        expires_at = time.time() + 3300
        return (access_token, expires_at)


__all__ = ["BoxJwtConfig", "BoxJwtAuth"]
