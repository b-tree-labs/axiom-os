# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""A colleague redeems from their own machine, with no access to the node.

The invitation primitive removed the administrator from holding other people's
keys. It did not remove them from the loop: redemption ran on the node, so
somebody still had to log in — and the people being onboarded are exactly the
people who have no node access. Developers here get no SSH and no database
credentials by design (ADR-002).

So the gate serves redemption. `POST /gate/redeem` with the code returns the key
once, to the holder.

This route mints a credential for whoever presents a valid code, so the code *is*
the authentication and the properties below are the whole security argument:

- it is single-use and expiring, enforced by the primitive, not re-implemented
  here;
- a refusal is a 403 that says which reason, for the same purpose as everywhere
  else on this surface — a colleague who cannot tell "already used" from
  "expired" has to go and ask somebody;
- neither the code nor the minted key is ever logged;
- it can only shrink: the role is resolved now, and a redemption that would grant
  more than was approved is refused.
"""

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path

import pytest

pytest.importorskip("fastapi")

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from axiom.webauth.invitations import (  # noqa: E402
    append_invitation,
    load_invitations,
    mint_invitation,
)


@pytest.fixture
def paths(tmp_path, monkeypatch) -> tuple[Path, Path]:
    keys = tmp_path / "api-keys.json"
    invites = tmp_path / "invitations.json"
    monkeypatch.setenv("AXIOM_GATE_API_KEYS_FILE", str(keys))
    monkeypatch.setenv("AXIOM_GATE_INVITATIONS_FILE", str(invites))
    return keys, invites


@pytest.fixture
def client(paths) -> TestClient:
    from axiom.extensions.builtins.webgate.api.routers import build_webgate_router

    app = FastAPI()
    app.include_router(build_webgate_router(oidc=None, secure_cookies=False))
    return TestClient(app)


def _invite(invites: Path, *, roles=("viewer",), scopes=("*:read",), expires=timedelta(days=7)):
    code, record = mint_invitation(
        principal="@someone:example",
        roles=roles,
        scopes=scopes,
        site="example",
        name="onboarding",
        invited_by="@approver:example",
        expires_in=expires,
    )
    append_invitation(invites, record)
    return code


def test_a_colleague_redeems_over_http_and_gets_a_key(client, paths):
    _keys, invites = paths
    code = _invite(invites)
    reply = client.post("/gate/redeem", json={"code": code})
    assert reply.status_code == 200, reply.text
    body = reply.json()
    assert body["token"].startswith("axk_")
    assert body["principal"] == "@someone:example"
    assert body["scopes"] == ["*:read"]


def test_the_key_is_stored_so_the_node_will_accept_it(client, paths):
    """Returning a token the node does not know would read as success and fail on
    first use, which is the worse of the two failures."""
    keys, invites = paths
    code = _invite(invites)
    token = client.post("/gate/redeem", json={"code": code}).json()["token"]

    from axiom.webauth.api_keys import JsonFileApiKeyStore

    identity = JsonFileApiKeyStore(keys).resolve(token)
    assert identity is not None, "the node does not recognise the key it just issued"
    assert identity.principal == "@someone:example"


def test_a_second_redemption_is_refused_and_says_why(client, paths):
    _keys, invites = paths
    code = _invite(invites)
    assert client.post("/gate/redeem", json={"code": code}).status_code == 200
    reply = client.post("/gate/redeem", json={"code": code})
    assert reply.status_code == 403
    assert "already redeemed" in reply.json()["detail"].lower()
    assert reply.json()["code"] == "invitation_refused"


def test_an_expired_invitation_is_refused_distinguishably(client, paths):
    _keys, invites = paths
    code = _invite(invites, expires=timedelta(seconds=-1))
    reply = client.post("/gate/redeem", json={"code": code})
    assert reply.status_code == 403
    assert "expired" in reply.json()["detail"].lower()


def test_an_unknown_code_is_refused(client, paths):
    _keys, invites = paths
    _invite(invites)
    reply = client.post("/gate/redeem", json={"code": "axi_inv_000000000000_nope"})
    assert reply.status_code == 403
    assert "not recognised" in reply.json()["detail"].lower()


def test_a_missing_code_says_what_is_missing(client, paths):
    reply = client.post("/gate/redeem", json={})
    assert reply.status_code == 400
    assert "code" in reply.json()["detail"].lower()


def test_a_body_that_is_not_json_is_refused_cleanly(client, paths):
    """Not a 500. This route is reachable without authentication, so a malformed
    body is an ordinary thing to receive."""
    reply = client.post(
        "/gate/redeem", content=b"not json", headers={"content-type": "application/json"}
    )
    assert reply.status_code in (400, 422), reply.text


def test_a_widened_role_is_refused_over_http_too(client, paths, monkeypatch):
    """The shrink-only rule has to hold on the route, not only in the library."""
    _keys, invites = paths
    code = _invite(invites, roles=("viewer",), scopes=("*:read",))

    from axiom.extensions.builtins.webgate import role_bundles

    class _Wider:
        def roles(self):
            return ("viewer",)

        def resolve(self, names):
            return ("*:read", "*:govern")

    monkeypatch.setattr(role_bundles, "default_bundle_registry", lambda: _Wider())
    reply = client.post("/gate/redeem", json={"code": code})
    assert reply.status_code == 403
    assert "govern" in reply.json()["detail"]


def test_neither_the_code_nor_the_key_is_logged(client, paths, caplog):
    """The route exists because a secret in a transcript is the exposure. A
    secret in the node's log is the same thing with a longer retention."""
    import logging

    _keys, invites = paths
    code = _invite(invites)
    with caplog.at_level(logging.DEBUG):
        token = client.post("/gate/redeem", json={"code": code}).json()["token"]
    logged = "\n".join(r.getMessage() for r in caplog.records)
    # The secret is everything after the fixed prefix. It is URL-safe base64,
    # so it can itself contain "_": taking the text after the LAST underscore
    # searched the log for a fragment as short as one character, which
    # matched unrelated text and failed about one run in fifteen.
    code_secret = code.split("_", 3)[3]  # axi_inv_<id>_<secret>
    key_secret = token.split("_", 2)[2]  # axk_<id>_<secret>
    assert code_secret not in logged, "the invitation code was logged"
    assert key_secret not in logged, "the minted key was logged"


def test_a_refused_redemption_leaves_the_invitation_alone(client, paths):
    _keys, invites = paths
    code = _invite(invites)
    client.post("/gate/redeem", json={"code": "axi_inv_000000000000_nope"})
    rows = load_invitations(invites)
    assert rows[0]["redeemed_at"] is None
    assert client.post("/gate/redeem", json={"code": code}).status_code == 200


def test_the_route_is_reachable_without_a_session(client, paths):
    """The whole point. A colleague has no account on the node yet — the
    invitation is what authenticates them, so requiring a session first would
    make this unusable by exactly the people it is for."""
    _keys, invites = paths
    code = _invite(invites)
    reply = client.post("/gate/redeem", json={"code": code})
    assert reply.status_code == 200
    assert "cookie" not in {k.lower() for k in reply.request.headers}


def test_the_reply_shape_is_what_a_client_can_act_on(client, paths):
    _keys, invites = paths
    code = _invite(invites)
    body = client.post("/gate/redeem", json={"code": code}).json()
    for field in ("token", "key_id", "principal", "scopes", "site"):
        assert field in body, f"the reply has no {field}"
    assert json.dumps(body)  # serialisable, no surprises
