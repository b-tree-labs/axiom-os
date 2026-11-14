# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""The gate logs each refusal: reason, code, request id and route; never the token (#1777).

A node refused a push every few minutes for a week and its serving log had no
line for any of them, so the refusal could not be seen from the node either.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter
from fastapi.testclient import TestClient

from axiom.extensions.builtins.http.compose import compose_app
from axiom.extensions.builtins.http.middleware import AuthzDecision, MiddlewareConfig
from axiom.extensions.builtins.http.registry import MountSpec, RouterRegistry

TOKEN = "axk_secretvalue_never_logged"


def _client():
    router = APIRouter()

    @router.post("/things/push")
    def push() -> dict:
        return {"ok": True}

    registry = RouterRegistry()
    registry.register(MountSpec(prefix="/things", router=router, extension="things"))

    def hook(request):
        return AuthzDecision(allow=False, reason="credential scopes do not cover this resource")

    app = compose_app(registry=registry, include_builtins=False,
                      middleware=MiddlewareConfig(authz=hook), auto_authz=False)
    return TestClient(app)


def test_a_refusal_is_logged_with_its_reason_and_request_id(caplog):
    with caplog.at_level(logging.INFO, logger="axi.serve"):
        response = _client().post("/things/push", headers={"Authorization": f"Bearer {TOKEN}",
                                                             "x-request-id": "req_seen"})
    assert response.status_code == 403
    refusals = [r for r in caplog.records
                if r.levelno == logging.WARNING and "refused" in r.getMessage()]
    assert refusals, [r.getMessage() for r in caplog.records]
    line = refusals[0].getMessage()
    assert "req_seen" in line
    assert "credential scopes do not cover this resource" in line
    assert "/things/push" in line and "things" in line
    assert TOKEN not in caplog.text


def _refused_line(caplog, headers):
    with caplog.at_level(logging.INFO, logger="axi.serve"):
        response = _client().post("/things/push", headers=headers)
    assert response.status_code == 403
    return next(r.getMessage() for r in caplog.records
                if r.levelno == logging.WARNING and "refused" in r.getMessage())


def test_the_line_names_the_key_id_it_saw_and_never_the_secret(caplog):
    """#1709: the node's log could not say which credential a refusal saw."""
    line = _refused_line(caplog, {"Authorization": "Bearer axk_0123456789ab_secretpart"})
    assert "credential=0123456789ab" in line
    assert "secretpart" not in caplog.text


def test_the_line_says_when_no_credential_arrived(caplog):
    assert "credential=none" in _refused_line(caplog, {})
