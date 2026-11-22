# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""A served error keeps the headers its route raised it with.

The shared error envelope rebuilt every ``HTTPException`` as a fresh JSON
response and dropped its headers. On a served node that removed the
``Retry-After`` from every 429 and 507 and the ``X-RateLimit-*`` budget from
every refusal, so a well-behaved client was told "no" and never "when": the
ingest face's rate limit was, in production, an instruction to retry at once.
Found 2026-10-08 driving a reconnect flood through a real node.
"""

from __future__ import annotations

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from axiom.extensions.builtins.http.middleware import MiddlewareConfig, install_middleware


def _app() -> TestClient:
    app = FastAPI()
    install_middleware(app, MiddlewareConfig(request_logging=False))

    @app.get("/busy")
    def busy():
        raise HTTPException(status_code=429, detail="slow down",
                            headers={"Retry-After": "7", "X-RateLimit-Remaining": "0"})

    return TestClient(app)


def test_a_refusal_keeps_its_retry_after_and_its_budget():
    r = _app().get("/busy")
    assert r.status_code == 429
    assert r.headers["Retry-After"] == "7"
    assert r.headers["X-RateLimit-Remaining"] == "0"
    assert r.json()["error"]["message"] == "slow down"
