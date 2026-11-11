# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""An HTTP request's envelope carries a capability for that request only.

The default envelope builder mints one token per request, for the request's own
``http.<verb>`` and ``extension://<mount><path>`` and nothing else, in place of
the wildcard test token. The token is built from the values it is checked
against, so the capability floor in ``decide()`` cannot refuse it on its own
request. What it refuses is the token moved to a different one.
"""

from __future__ import annotations

import hashlib
from dataclasses import replace
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from axiom.extensions.builtins.authz.decide import DecideContext, decide
from axiom.extensions.builtins.authz.rules import Rule
from axiom.extensions.builtins.http.authz_hook import build_authz_hook, default_envelope_builder
from axiom.extensions.builtins.http.middleware import MiddlewareConfig, install_middleware
from axiom.governance import (
    ActionIntent,
    Decision,
    IntentPattern,
    ResourcePattern,
    ResourceRef,
    Verdict,
)
from axiom.vega.identity.principal import Principal


def _request(path: str, method: str = "GET", mount: str = "chat") -> SimpleNamespace:
    return SimpleNamespace(
        method=method,
        url=SimpleNamespace(path=path),
        state=SimpleNamespace(mount_extension=mount),
    )


def _who() -> Principal:
    handle = "@alice:site"
    return Principal(handle=handle, public_bytes=hashlib.sha256(handle.encode()).digest())


def test_the_token_names_the_requests_own_verb_and_resource():
    env = default_envelope_builder(_request("/x"), _who())
    cap = env.capability
    assert cap.intent_pattern.value == "http.read"
    assert cap.resource_pattern.value == "extension://chat/x"
    assert cap.subject == env.actor
    assert cap.permits_intent(env.intent)
    assert cap.permits_resource(env.resource)


def test_the_token_does_not_cover_another_verb_or_mount():
    cap = default_envelope_builder(_request("/x"), _who()).capability
    assert not cap.permits_intent(ActionIntent("http.invoke"))
    assert not cap.permits_resource(ResourceRef.parse("extension://other/x"))
    assert not cap.permits_resource(ResourceRef.parse("extension://chat/x2"))


def test_a_write_gets_a_write_token():
    cap = default_envelope_builder(_request("/x", "POST"), _who()).capability
    assert cap.intent_pattern.value == "http.invoke"
    assert not cap.permits_intent(ActionIntent("http.read"))


def test_the_floor_refuses_the_token_on_another_resource():
    # A permit-everything rule would allow it; the floor answers first.
    ctx = DecideContext()
    ctx.add_rule(
        Rule(
            name="permit_all_http",
            intent_pattern=IntentPattern("http.*"),
            actor_pattern="*",
            resource_pattern=ResourcePattern("*"),
            disposition="permit",
        )
    )
    env = default_envelope_builder(_request("/x"), _who())
    assert decide(env, ctx).decision is Decision.PERMIT
    moved = replace(env, resource=ResourceRef.parse("extension://other/x"))
    verdict = decide(moved, ctx)
    assert verdict.decision is Decision.DENY
    assert verdict.reason == "capability does not permit this resource"
    assert verdict.receipt_fragment_id


class TestAPathEndingInAStar:
    """``for_call`` refuses a resource ending in ``*``, because the matcher reads it
    as a prefix. Over HTTP that has to be a plain refusal, not a server error: the
    path is the caller's to choose, and ``%2A`` decodes to a star before the
    envelope is built."""

    @pytest.fixture
    def app(self):
        app = FastAPI()
        app.state.decisions = []
        app.state.reached = []

        def permit_all(envelope):
            app.state.decisions.append(envelope.resource)
            return Verdict.from_decision(Decision.PERMIT, "permit all", "receipt-1")

        install_middleware(
            app,
            MiddlewareConfig(
                authz=build_authz_hook(
                    resolve_principal=lambda _request: _who(),
                    decide_fn=permit_all,
                )
            ),
        )

        @app.api_route("/chat/{rest:path}", methods=["GET", "POST"])
        def handler(rest: str):
            app.state.reached.append(rest)
            return {"ok": True}

        return app

    @pytest.mark.parametrize("path", ["/chat/%2A", "/chat/x%2a", "/chat/*", "/chat/a/b*"])
    @pytest.mark.parametrize("method", ["GET", "POST"])
    def test_it_is_a_clean_deny(self, app, path, method):
        response = TestClient(app, raise_server_exceptions=True).request(method, path)

        assert response.status_code == 403
        assert response.json()["error"]["code"] == "forbidden"
        assert app.state.decisions == []
        assert app.state.reached == []

    def test_a_star_inside_a_path_is_not_refused(self, app):
        """Only a trailing star is a prefix."""
        response = TestClient(app).get("/chat/a*b")

        assert response.status_code == 200
        assert app.state.reached == ["a*b"]
