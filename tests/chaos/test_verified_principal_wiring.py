# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Security: the verified principal reaches the handler, and only when allowed.

A mutation sweep removed the line that stashes it and every test still passed —
the chat tests supply the principal directly, so nothing covered the wiring
that produces it. A defence nobody tests end to end is a defence that can be
deleted by accident.
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.testclient import TestClient

from axiom.extensions.builtins.http.compose import compose_app
from axiom.extensions.builtins.http.middleware import AuthzDecision, MiddlewareConfig
from axiom.extensions.builtins.http.registry import MountSpec, RouterRegistry


# MountSpec's prefix is the namespace claim; the router itself is mounted
# without re-prefixing, so the path here is "/who".
def _echo_router() -> APIRouter:
    router = APIRouter()

    @router.get("/who")
    def who(request: Request) -> dict:
        from axiom.extensions.builtins.http.chat_server import (
            verified_principal_of,
        )

        return {"principal": verified_principal_of(request)}

    return router


def _client(hook) -> TestClient:
    registry = RouterRegistry()
    registry.register(MountSpec("/t", _echo_router(), "demo"))
    return TestClient(
        compose_app(
            registry=registry,
            include_builtins=False,
            middleware=MiddlewareConfig(authz=hook),
            auto_authz=False,
        )
    )


class TestAnAllowedRequestCarriesItsPrincipal:
    def test_the_handler_sees_the_resolved_principal(self):
        client = _client(
            lambda request: AuthzDecision(allow=True, principal="@alice:example")
        )
        response = client.get("/who")
        assert response.status_code == 200
        assert response.json()["principal"] == "@alice:example"

    def test_a_different_credential_yields_a_different_principal(self):
        """Pinned so the wiring cannot return one constant and look right."""
        client = _client(
            lambda request: AuthzDecision(allow=True, principal="@bob:example")
        )
        assert client.get("/who").json()["principal"] == "@bob:example"


class TestADeniedRequestNeverReachesTheHandler:
    def test_denial_is_a_403(self):
        client = _client(lambda request: AuthzDecision(allow=False, reason="nope"))
        assert client.get("/who").status_code == 403

    def test_a_denied_principal_is_never_published(self):
        """The stash happens after the deny-return. A principal named on a
        REFUSED decision must not reach a handler, or a rejected credential
        would still choose whose conversation is opened."""
        client = _client(
            lambda request: AuthzDecision(
                allow=False, reason="nope", principal="@alice:example"
            )
        )
        response = client.get("/who")
        assert response.status_code == 403
        assert "@alice:example" not in response.text


class TestAnAllowWithoutAPrincipalIsHarmless:
    def test_no_principal_means_no_identity(self):
        """An authz layer that allows without resolving a person — a shared
        API key — leaves the handler to fall back to a claimed identity, which
        is namespaced."""
        client = _client(lambda request: AuthzDecision(allow=True))
        assert client.get("/who").json()["principal"] == ""
