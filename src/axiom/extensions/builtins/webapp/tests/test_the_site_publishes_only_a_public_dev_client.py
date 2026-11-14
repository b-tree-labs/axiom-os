# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""``GET /api/v1/site/sign-in`` publishes a public dev client and nothing else (#1717).

A contributor's local node bound to this site asks it for the sign-in issuer
and public client id. Through the composed app and the real authz middleware:
a developer key (``*:read``) reads it, nothing anonymous does, a node that has
not turned it on answers 404, and the answer is four allow-listed fields.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from axiom.extensions.builtins.http.authz_hook import build_authz_hook, build_bearer_resolver
from axiom.extensions.builtins.http.compose import compose_app
from axiom.extensions.builtins.http.middleware import MiddlewareConfig
from axiom.extensions.builtins.http.registry import RouterRegistry
from axiom.extensions.builtins.webapp.mount import mount_spec
from axiom.extensions.builtins.webgate import dev_client
from axiom.governance import Decision, Verdict

HOME = "example-home"
TENANT = "00000000-0000-0000-0000-0000000000aa"
NODE_APP = "00000000-0000-0000-0000-0000000000cc"
DEV_APP = "00000000-0000-0000-0000-0000000000dd"
SECRET = "do-not-publish-this-value"


class _Keys:
    def __init__(self, table):
        self._table = table

    def resolve(self, token):
        found = self._table.get(token)
        if found is None:
            return None
        principal, scopes, site = found
        return SimpleNamespace(
            principal=principal, scopes=scopes, key_id=token[-4:], name="k", site=site
        )


KEYS = {"axk_dev_reader": ("@dev:example-home", ("*:read",), HOME)}


def _permit(_envelope):
    return Verdict.from_decision(Decision.PERMIT, "test permits", f"t-{uuid.uuid4().hex}")


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("AXIOM_SITE", HOME)
    for key in ("AXIOM_SERVED_SITES", "AXIOM_HOME_SITES", dev_client.ENV_CLIENT_ID,
                dev_client.ENV_ISSUER, dev_client.ENV_PUBLIC_SAFE):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("AXIOM_GATE_OIDC_PROVIDER", "entra")
    monkeypatch.setenv("AXIOM_GATE_OIDC_TENANT", TENANT)
    monkeypatch.setenv("AXIOM_GATE_OIDC_CLIENT_ID", NODE_APP)
    monkeypatch.setenv("AXIOM_GATE_OIDC_CLIENT_SECRET", SECRET)
    registry = RouterRegistry()
    registry.register(mount_spec())
    hook = build_authz_hook(
        resolve_principal=build_bearer_resolver({}, api_keys=_Keys(KEYS)), decide_fn=_permit
    )
    app = compose_app(
        registry=registry,
        include_builtins=False,
        middleware=MiddlewareConfig(authz=hook),
        auto_authz=False,
    )
    return TestClient(app)


def _get(client, key="axk_dev_reader"):
    return client.get(
        "/api/v1/site/sign-in", headers={"Authorization": f"Bearer {key}"} if key else {}
    )


def test_without_a_key_it_is_refused(client, monkeypatch):
    monkeypatch.setenv(dev_client.ENV_CLIENT_ID, DEV_APP)
    assert _get(client, key=None).status_code == 403


def test_a_node_that_has_not_turned_it_on_answers_404(client):
    r = _get(client)
    assert r.status_code == 404
    assert "not enabled SSO for local development" in r.text


def test_a_developer_key_reads_exactly_the_four_public_fields(client, monkeypatch):
    monkeypatch.setenv(dev_client.ENV_CLIENT_ID, DEV_APP)
    r = _get(client)
    assert r.status_code == 200, r.text
    body = r.json()
    assert set(body) == {"issuer_url", "client_id", "client_type", "allowed_redirects"}
    assert body == {
        "issuer_url": f"https://login.microsoftonline.com/{TENANT}/v2.0",
        "client_id": DEV_APP,
        "client_type": "public",
        "allowed_redirects": [
            "http://localhost/gate/oidc/callback",
            "http://127.0.0.1/gate/oidc/callback",
        ],
    }
    assert SECRET not in r.text and NODE_APP not in r.text


def test_the_nodes_own_confidential_app_is_not_published_unless_marked_public_safe(
    client, monkeypatch
):
    monkeypatch.setenv(dev_client.ENV_CLIENT_ID, NODE_APP)
    assert _get(client).status_code == 404
    monkeypatch.setenv(dev_client.ENV_PUBLIC_SAFE, "1")
    r = _get(client)
    assert r.status_code == 200 and r.json()["client_id"] == NODE_APP
    assert SECRET not in r.text


def test_an_explicit_issuer_wins_and_a_plain_http_one_publishes_nothing(monkeypatch):
    env = {dev_client.ENV_CLIENT_ID: DEV_APP, dev_client.ENV_ISSUER: "https://id.example.org"}
    assert dev_client.published(env)["issuer_url"] == "https://id.example.org"
    assert dev_client.published({**env, dev_client.ENV_ISSUER: "http://id.example.org"}) is None
    assert dev_client.published({dev_client.ENV_CLIENT_ID: DEV_APP}) is None
