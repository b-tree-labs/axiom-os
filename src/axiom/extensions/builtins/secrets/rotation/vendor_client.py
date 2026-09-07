# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Minimal JSON-over-HTTPS client for vendor key-management APIs (#16 / ADR-095).

Vendor-API rotation (SendGrid, GitLab, LangSmith, …) mints and revokes
credentials over REST. Rather than pull a third-party HTTP dependency into the
secrets extension, this is a thin ``urllib`` client: ``post``/``get``/``delete``
returning parsed JSON, carrying the vendor admin credential in an auth header.

Redaction is a hard requirement here, not a nicety. The admin credential *and*
every response body are secret — a mint response literally is the new key — so
neither is ever placed in an exception or log line. A failed request surfaces
only the method, path, and HTTP status.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any, Callable

_Opener = Callable[..., Any]


class VendorHTTPError(RuntimeError):
    """A vendor API call failed (non-2xx or transport error).

    Deliberately carries no response body and no credential — only enough to
    say which call failed and with what status.
    """


class VendorHttpClient:
    """Auth-carrying JSON client for a single vendor base URL.

    ``token`` is the vendor admin credential, resolved through the SecretStore
    seam by the caller (see ``rotation.config``) — never inlined. ``opener`` is
    a test seam defaulting to :func:`urllib.request.urlopen`.
    """

    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        auth_header: str = "Authorization",
        auth_scheme: str = "Bearer",
        timeout: float = 15.0,
        opener: _Opener | None = None,
    ) -> None:
        self._base = base_url.rstrip("/")
        scheme = auth_scheme.strip()
        self._auth = f"{scheme} {token}" if scheme else token
        self._auth_header = auth_header
        self._timeout = timeout
        self._opener = opener or urllib.request.urlopen

    def post(self, path: str, body: dict) -> dict:
        return self._request("POST", path, body)

    def get(self, path: str) -> dict:
        return self._request("GET", path, None)

    def delete(self, path: str) -> None:
        self._request("DELETE", path, None)

    def _request(self, method: str, path: str, body: dict | None) -> dict:
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(f"{self._base}{path}", data=data, method=method)
        req.add_header(self._auth_header, self._auth)
        if data is not None:
            req.add_header("Content-Type", "application/json")
        try:
            resp = self._opener(req, timeout=self._timeout)
            raw = resp.read()
        except urllib.error.HTTPError as exc:
            # exc wraps the response body — intentionally not surfaced.
            raise VendorHTTPError(f"{method} {path} -> HTTP {exc.code}") from None
        except urllib.error.URLError as exc:
            raise VendorHTTPError(f"{method} {path} failed: {exc.reason}") from None
        if not raw:
            return {}
        try:
            return json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            return {}


__all__ = ["VendorHttpClient", "VendorHTTPError"]
