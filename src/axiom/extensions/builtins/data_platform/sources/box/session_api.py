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
from collections.abc import Callable
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
        # On 429, honor Box's Retry-After and retry transparently instead of
        # failing the item. Box allows ~1000 req/min/user; running a worker
        # pool near that ceiling produces transient 429s — retrying (vs hard
        # fail) is what lets us run hot without leaking items. Override via
        # BOX_MAX_RETRIES.
        try:
            self._max_retries = int(os.environ.get("BOX_MAX_RETRIES", "5"))
        except ValueError:
            self._max_retries = 5

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

        # A server token (jwt_auth / bearer / BOX_DEVELOPER_TOKEN) authenticates
        # on its own — the SSO state.json is only REQUIRED when cookies are the
        # only credential. Requiring it unconditionally is why the Dagster
        # box_corpus_sensor died with "No Box session" despite a valid
        # BOX_DEVELOPER_TOKEN. Load cookies when present; never demand them when
        # a token is available.
        has_token = self._jwt_auth is not None or bool(self._bearer)
        state_file = self.session_dir / "state.json"
        if not state_file.exists():
            if not has_token:
                raise RuntimeError(
                    f"No Box session at {state_file} and no token configured. "
                    "Provide a server credential (CCG/JWT/OAuth via "
                    "jwt_secret_ref, or BOX_DEVELOPER_TOKEN), or run "
                    "`axi pub push --endpoint box-browser --headed <any-file>` "
                    "once to capture an SSO session."
                )
            state = {}
        else:
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

    def _retry_on_429(self, fn: Callable[[], Any]) -> Any:
        """Run ``fn``; on a 429 (typed :class:`RateLimited`) honor the
        server's Retry-After and retry, up to ``self._max_retries`` times.

        This is what lets a worker pool run near Box's ~1000 req/min/user
        ceiling without leaking items: a transient 429 becomes a short
        sleep + retry instead of a hard failure. Exponential fallback
        backoff when the server gives no explicit Retry-After. Re-raises
        after the budget is exhausted (then it's a real failure)."""
        from axiom.infra.ratelimit import RateLimited, sleep_for_retry
        for attempt in range(self._max_retries + 1):
            try:
                return fn()
            except RateLimited as exc:
                if attempt >= self._max_retries:
                    raise
                # Retry-After wins; otherwise exponential backoff (2,4,8,…s).
                sleep_for_retry(exc.window,
                                default_backoff_s=min(2 ** (attempt + 1), 60))

    def get_json(self, path: str, params: dict[str, Any] | None = None,
                 *, if_none_match: str | None = None) -> dict[str, Any] | None:
        """``GET <path>`` against the Box REST API.

        ``if_none_match`` sends an ``If-None-Match: <etag>`` header; if
        Box responds 304, returns ``None`` (the caller's cached copy is
        still good — skip the byte-level work).

        On 429, transparently honors Retry-After and retries (up to
        ``self._max_retries``); only after the retry budget is exhausted
        does the :class:`RateLimited` propagate. Other non-2xx statuses
        raise ``RuntimeError`` with the body's first 200 chars.
        """
        return self._retry_on_429(
            lambda: self._get_json_once(path, params, if_none_match=if_none_match))

    def _auth_headers(self, if_none_match: str | None = None) -> dict[str, str] | None:
        """Per-request headers with a FRESH Authorization from the auth
        provider. The session-default header set at _open() goes stale on long
        runs (access tokens live ~60 min); reading the provider per request lets
        it mint/refresh a token transparently — fixes mid-run 401s."""
        h: dict[str, str] = {}
        if if_none_match:
            h["If-None-Match"] = if_none_match
        if self._jwt_auth is not None:
            h["Authorization"] = self._jwt_auth.authorization_header()
        return h or None

    def _get_json_once(self, path: str, params: dict[str, Any] | None = None,
                       *, if_none_match: str | None = None,
                       _retried: bool = False) -> dict[str, Any] | None:
        if self._session is None:
            self._open()
        url = _BOX_API_BASE + self._norm(path)
        r = self._session.get(url, params=params, timeout=60,
                              allow_redirects=True,
                              headers=self._auth_headers(if_none_match))
        self._update_window(r)
        if r.status_code == 304:
            return None
        if r.status_code == 429:
            from axiom.infra.ratelimit import RateLimited, parse_headers
            raise RateLimited(parse_headers(r.headers))
        if r.status_code == 401 and self._jwt_auth is not None and not _retried:
            # Token revoked/expired despite the fresh read — force a refresh
            # and retry once before giving up.
            inv = getattr(self._jwt_auth, "invalidate", None)
            if inv:
                inv()
            return self._get_json_once(path, params,
                                       if_none_match=if_none_match, _retried=True)
        if not r.ok:
            raise RuntimeError(
                f"Box GET {path} failed: {r.status_code} {r.text[:200]}"
            )
        return r.json()

    def get_bytes(self, path: str, *,
                  if_none_match: str | None = None) -> bytes | None:
        """``GET <path>`` returning raw bytes (file content).

        Same 304 / retry-on-429 / error semantics as :meth:`get_json`.
        """
        return self._retry_on_429(
            lambda: self._get_bytes_once(path, if_none_match=if_none_match))

    def _get_bytes_once(self, path: str, *,
                        if_none_match: str | None = None,
                        _retried: bool = False) -> bytes | None:
        if self._session is None:
            self._open()
        url = _BOX_API_BASE + self._norm(path)
        # /content endpoints 302 to presigned download URLs; let requests follow.
        r = self._session.get(url, timeout=300, allow_redirects=True,
                              headers=self._auth_headers(if_none_match))
        self._update_window(r)
        if r.status_code == 304:
            return None
        if r.status_code == 429:
            from axiom.infra.ratelimit import RateLimited, parse_headers
            raise RateLimited(parse_headers(r.headers))
        if r.status_code == 401 and self._jwt_auth is not None and not _retried:
            inv = getattr(self._jwt_auth, "invalidate", None)
            if inv:
                inv()
            return self._get_bytes_once(path, if_none_match=if_none_match,
                                        _retried=True)
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
