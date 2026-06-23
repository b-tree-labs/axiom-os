# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``BoxSessionApiClient`` — pure-Python Box REST client backed by a
captured browser session.

Companion to :class:`BoxBrowserApiClient` (which uses Playwright at
runtime). This client only needs ``requests`` + the cookies inside the
captured Playwright ``storage_state`` JSON — so the production daemon
pod does NOT need Chromium, system libs, or 100+MB of headless-browser
download. Capture still uses Playwright on the operator's laptop.

The Box web UI authenticates against ``api.box.com`` with the same
session cookies (``.box.com`` scope) it sets on ``app.box.com`` —
replaying those cookies from a pure-Python ``requests.Session``
authorizes the call.

Implements the same minimal :class:`BoxApiClient` protocol
(``get_json`` + ``get_bytes``) so :class:`BoxSourceProvider` can swap
between this and the browser client without touching call sites.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

_BOX_API_BASE = "https://api.box.com/2.0"

# Mimic a recent Chrome UA so Box doesn't reject the call as a bot;
# the captured cookies were created by a Chrome session.
_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
)


class BoxSessionApiClient:
    """Box REST client driven by a captured Playwright storage_state.

    Construction takes a ``session_dir`` containing ``state.json``. The
    session is lazily opened on the first call. Use as a context
    manager when batching:

    .. code-block:: python

        with BoxSessionApiClient(session_dir=p) as c:
            meta = c.get_json("/files/123")
            blob = c.get_bytes("/files/123/content")
    """

    def __init__(self, *, session_dir: Path, bearer_token: str | None = None,
                 jwt_auth: Any = None) -> None:
        self.session_dir = session_dir
        # Auth precedence:
        #   1. jwt_auth (BoxJwtAuth) — server-to-server, auto-refreshing
        #   2. bearer_token (explicit kwarg)
        #   3. BOX_DEVELOPER_TOKEN env var (60-min dev token from developer console)
        #
        # JWT auth ends the 60-min token cliff: every request reads a fresh
        # access token from the cached BoxJwtAuth; the auth provider mints
        # + refreshes them automatically. Daemon never needs human input.
        import os
        self._jwt_auth = jwt_auth
        self._bearer = bearer_token or os.environ.get("BOX_DEVELOPER_TOKEN") or None
        self._session: Any = None  # requests.Session

        # Most-recent rate-limit window parsed from any response. Callers
        # (and PLINTH) inspect to decide whether to slow down before the
        # next call. ``None`` until the first response lands.
        from axiom.infra.ratelimit import RateLimitWindow
        self.last_window: RateLimitWindow | None = None

    # ---- context-manager lifecycle --------------------------------------

    def __enter__(self) -> BoxSessionApiClient:
        self._open()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def _open(self) -> None:
        try:
            import requests
        except ImportError as exc:  # pragma: no cover - environment guard
            raise RuntimeError(
                "`requests` not installed; required for BoxSessionApiClient"
            ) from exc

        state_file = self.session_dir / "state.json"
        if not state_file.exists():
            raise RuntimeError(
                f"No Box session at {state_file}. "
                "Run `axi pub push --endpoint box-browser --headed <any-file>` "
                "once to capture an SSO session."
            )

        state = json.loads(state_file.read_text())
        s = requests.Session()
        for c in state.get("cookies", []):
            # storage_state JSON uses Playwright's cookie shape:
            # {name, value, domain, path, expires, httpOnly, secure, sameSite}
            try:
                s.cookies.set(
                    name=c["name"],
                    value=c["value"],
                    domain=c.get("domain"),
                    path=c.get("path", "/"),
                    secure=bool(c.get("secure", False)),
                )
            except Exception:  # noqa: BLE001 — skip malformed cookies, keep going
                continue
        headers = {
            "Accept": "application/json",
            "User-Agent": _USER_AGENT,
        }
        if self._jwt_auth is not None:
            # JWT auth — fresh token per session-open; per-request
            # refresh is wired by the rate-limit / fetch wrapper that
            # consults self._jwt_auth.authorization_header() each call.
            headers["Authorization"] = self._jwt_auth.authorization_header()
        elif self._bearer:
            headers["Authorization"] = f"Bearer {self._bearer}"
        s.headers.update(headers)
        self._session = s

    def close(self) -> None:
        if self._session is not None:
            try:
                self._session.close()
            finally:
                self._session = None

    # ---- BoxApiClient ---------------------------------------------------

    def get_json(self, path: str, params: dict[str, Any] | None = None,
                 *, if_none_match: str | None = None) -> dict[str, Any] | None:
        """``GET <path>`` against the Box REST API.

        ``if_none_match`` sends an ``If-None-Match: <etag>`` header; if
        Box responds 304, returns ``None`` (the caller's cached copy is
        still good — skip the byte-level work).

        On 429, raises a typed :class:`RateLimited` carrying the parsed
        :class:`RateLimitWindow` so the caller can ``sleep_for_retry``
        and resume. Other non-2xx statuses raise ``RuntimeError`` with
        the body's first 200 chars.
        """
        if self._session is None:
            self._open()
        url = _BOX_API_BASE + self._norm(path)
        headers = {"If-None-Match": if_none_match} if if_none_match else None
        r = self._session.get(url, params=params, timeout=60,
                              allow_redirects=True, headers=headers)
        self._update_window(r)
        if r.status_code == 304:
            return None
        if r.status_code == 429:
            from axiom.infra.ratelimit import RateLimited, parse_headers
            raise RateLimited(parse_headers(r.headers))
        if not r.ok:
            raise RuntimeError(
                f"Box GET {path} failed: {r.status_code} {r.text[:200]}"
            )
        return r.json()

    def get_bytes(self, path: str, *,
                  if_none_match: str | None = None) -> bytes | None:
        """``GET <path>`` returning raw bytes (file content).

        Same 304 / 429 / error semantics as :meth:`get_json`.
        """
        if self._session is None:
            self._open()
        url = _BOX_API_BASE + self._norm(path)
        headers = {"If-None-Match": if_none_match} if if_none_match else None
        # /content endpoints 302 to presigned download URLs; let requests follow.
        r = self._session.get(url, timeout=300, allow_redirects=True,
                              headers=headers)
        self._update_window(r)
        if r.status_code == 304:
            return None
        if r.status_code == 429:
            from axiom.infra.ratelimit import RateLimited, parse_headers
            raise RateLimited(parse_headers(r.headers))
        if not r.ok:
            raise RuntimeError(
                f"Box GET {path} (bytes) failed: {r.status_code} {r.text[:200]}"
            )
        return r.content

    def _update_window(self, response: Any) -> None:
        """Parse and store the rate-limit window from any response."""
        from axiom.infra.ratelimit import parse_headers
        try:
            self.last_window = parse_headers(response.headers or {})
        except Exception:  # noqa: BLE001 — header parsing must never raise
            pass

    # ---- internals ------------------------------------------------------

    @staticmethod
    def _norm(path: str) -> str:
        return path if path.startswith("/") else "/" + path


__all__ = ["BoxSessionApiClient"]
