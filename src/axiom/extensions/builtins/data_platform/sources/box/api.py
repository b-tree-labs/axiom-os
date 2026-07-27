# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``BoxBrowserApiClient`` — the production Box API client for
:class:`BoxIngestSource`.

Wraps the same Playwright + session pattern the publishing extension
uses for *uploads* (``providers/storage/box_browser.py``), and uses it
for the *download* direction. The Box REST call is issued from inside
the authenticated browser context (``page.evaluate``), so no developer
API keys are needed — the user's SSO + MFA session, captured once
``--headed``, drives every subsequent call.

This module is not exercised by the source's unit tests (those use a
``FakeBoxApi`` stub). It is end-to-end exercised by the post-deploy
smoke test in the consumer's deployment runbook.
"""

from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import Any

_BOX_API_BASE = "https://api.box.com/2.0"


class BoxBrowserApiClient:
    """Box REST client backed by a Playwright session.

    Construction takes a ``session_dir`` pointing at the storage_state
    JSON written by ``publishing/box_browser`` after a successful
    ``--headed`` login. The same state.json is reused; running this
    client never mutates it.

    Page-evaluate is opened lazily on the first call and closed on
    ``__exit__``. Use as a context manager when ingesting batches.
    """

    def __init__(self, *, session_dir: Path, headless: bool = True) -> None:
        self.session_dir = session_dir
        self.headless = headless
        self._pw = None
        self._browser = None
        self._context = None
        self._page = None

    # ---- context-manager lifecycle --------------------------------------

    def __enter__(self) -> BoxBrowserApiClient:
        self._open()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def _open(self) -> None:
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:  # pragma: no cover - environment guard
            raise RuntimeError(
                "Playwright not installed. Run:\n"
                "  pip install playwright && playwright install chromium"
            ) from exc

        state_file = self.session_dir / "state.json"
        if not state_file.exists():
            raise RuntimeError(
                f"No Box session at {state_file}. "
                "Run `axi pub push --storage box-browser --headed <any-file>` "
                "once to capture an SSO session."
            )

        self._pw = sync_playwright().start()
        self._browser = self._pw.chromium.launch(headless=self.headless)
        self._context = self._browser.new_context(storage_state=str(state_file))
        self._page = self._context.new_page()
        # Park on app.box.com so the cookie scope applies to api.box.com fetches.
        self._page.goto("https://app.box.com", wait_until="domcontentloaded", timeout=30000)

    def close(self) -> None:
        try:
            if self._context is not None:
                self._context.close()
            if self._browser is not None:
                self._browser.close()
        finally:
            if self._pw is not None:
                self._pw.stop()
            self._pw = None
            self._browser = None
            self._context = None
            self._page = None

    # ---- BoxApiClient ---------------------------------------------------

    def get_json(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        if self._page is None:
            self._open()
        assert self._page is not None
        url = self._url(path, params)
        result = self._page.evaluate(
            """async (url) => {
                const r = await fetch(url, { headers: { 'Accept': 'application/json' } });
                if (!r.ok) return { __error: true, status: r.status, text: await r.text() };
                return await r.json();
            }""",
            url,
        )
        if isinstance(result, dict) and result.get("__error"):
            raise RuntimeError(f"Box GET {path} failed: {result['status']} {result.get('text', '')[:200]}")
        return result

    def get_bytes(self, path: str) -> bytes:
        if self._page is None:
            self._open()
        assert self._page is not None
        url = self._url(path)
        # Box's /content endpoint 302-redirects to a presigned download URL.
        # page.evaluate's fetch follows redirects automatically; we base64
        # back over the JS boundary to preserve byte fidelity.
        b64 = self._page.evaluate(
            """async (url) => {
                const r = await fetch(url, { redirect: 'follow' });
                if (!r.ok) return { __error: true, status: r.status, text: await r.text() };
                const buf = await r.arrayBuffer();
                let bin = '';
                const bytes = new Uint8Array(buf);
                const chunk = 0x8000;
                for (let i = 0; i < bytes.length; i += chunk) {
                    bin += String.fromCharCode.apply(null, bytes.subarray(i, i + chunk));
                }
                return btoa(bin);
            }""",
            url,
        )
        if isinstance(b64, dict) and b64.get("__error"):
            raise RuntimeError(f"Box GET {path} (bytes) failed: {b64['status']} {b64.get('text', '')[:200]}")
        return base64.b64decode(b64)

    # ---- internals ------------------------------------------------------

    @staticmethod
    def _url(path: str, params: dict[str, Any] | None = None) -> str:
        if not path.startswith("/"):
            path = "/" + path
        url = _BOX_API_BASE + path
        if params:
            qs = "&".join(f"{k}={json.dumps(v) if not isinstance(v, str) else v}" for k, v in params.items())
            sep = "&" if "?" in url else "?"
            url = f"{url}{sep}{qs}"
        return url


__all__ = ["BoxBrowserApiClient"]
