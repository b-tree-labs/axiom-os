# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Stdlib-only HTTP transport for the classroom-join ceremony.

Tier A PR 6 — the production implementation of the :class:`Transport`
protocol from ``classroom_client.py``. Uses :mod:`urllib.request` so
Axiom's required-deps footprint doesn't grow.

Tests keep using ``InProcessTransport`` from PR 5 — that's faster and
hermetic. This module is exercised via one "real HTTP" integration
test that spins an ``http.server.HTTPServer`` in a background thread.
"""

from __future__ import annotations

import urllib.error
import urllib.request
from dataclasses import dataclass


@dataclass
class UrllibTransport:
    """``Transport`` implementation backed by :mod:`urllib.request`.

    Implements both the join-side ``Transport`` (POST) and the
    materials-side ``GetTransport`` (GET), so callers that need both
    can share a single instance. Kept deliberately minimal — no
    retries, no auth, no compression; callers build those on top.
    """

    timeout_s: float = 30.0

    def post(self, url: str, body: str) -> tuple[int, str]:
        req = urllib.request.Request(
            url,
            data=body.encode("utf-8"),
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout_s) as resp:
                status = resp.status
                resp_body = resp.read().decode("utf-8", errors="replace")
            return status, resp_body
        except urllib.error.HTTPError as exc:
            resp_body = exc.read().decode("utf-8", errors="replace") if exc.fp else ""
            return exc.code, resp_body

    def get(self, url: str) -> tuple[int, bytes]:
        req = urllib.request.Request(url, method="GET")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout_s) as resp:
                return resp.status, resp.read()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read() if exc.fp else b""


__all__ = ["UrllibTransport"]
