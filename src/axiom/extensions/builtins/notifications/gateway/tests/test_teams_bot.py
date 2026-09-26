# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0

"""Bot Framework messaging endpoint (HERALD-2b): JWT seam + Activity dispatch.

Fail-closed contract — a fake verifier that accepts routes the Activity into
``TeamsInteractiveChannel.dispatch``; a fake verifier that rejects returns 401
and never touches the channel. No MS creds, no network, no ``PyJWT``."""

from __future__ import annotations

import json

from fastapi import FastAPI
from fastapi.testclient import TestClient

from axiom.extensions.builtins.notifications.channels.teams_interactive import (
    TeamsInteractiveChannel,
)
from axiom.extensions.builtins.notifications.gateway.teams_bot import (
    build_teams_bot_router,
)


class _FakeVerifier:
    def __init__(self, ok: bool):
        self._ok = ok
        self.calls = 0

    def verify(self, *, headers, body) -> bool:
        self.calls += 1
        return self._ok


def _channel():
    # token/poster injected so the channel never reaches for the SDK/network.
    return TeamsInteractiveChannel(
        app_id="app-guid",
        app_password="secret",
        service_url="https://smba.example/amer",
        poster=object(),
        token_provider=lambda: "t",
    )


def _client(channel, verifier):
    app = FastAPI()
    app.include_router(build_teams_bot_router(channel=channel, verifier=verifier))
    return TestClient(app)


_MESSAGE = {
    "type": "message",
    "text": "<at>AXI</at> what is the coolant temp?",
    "from": {"id": "29:user"},
    "conversation": {"id": "19:room@thread.tacv2;messageid=1"},
}


def test_valid_jwt_routes_activity_to_channel_dispatch():
    channel = _channel()
    seen = []
    channel.on_message(seen.append)
    verifier = _FakeVerifier(ok=True)
    c = _client(channel, verifier)

    r = c.post(
        "/herald/inbound/teams-bot",
        content=json.dumps(_MESSAGE).encode(),
        headers={"authorization": "Bearer good-token", "content-type": "application/json"},
    )
    assert r.status_code == 200
    assert r.json()["status"] == "accepted"
    assert verifier.calls == 1
    # Activity was parsed + dispatched: handler saw the mention-stripped text.
    assert len(seen) == 1
    assert seen[0].text == "what is the coolant temp?"


def test_invalid_jwt_rejected_401_and_handler_not_called():
    channel = _channel()
    seen = []
    channel.on_message(seen.append)
    verifier = _FakeVerifier(ok=False)
    c = _client(channel, verifier)

    r = c.post(
        "/herald/inbound/teams-bot",
        content=json.dumps(_MESSAGE).encode(),
        headers={"authorization": "Bearer forged", "content-type": "application/json"},
    )
    assert r.status_code == 401
    assert r.json()["status"] == "bad_jwt"
    assert seen == []  # fail-closed: channel never touched


def test_absent_jwt_rejected_401():
    # No Authorization header at all — the default BotFrameworkJwtVerifier
    # returns False; here the fake mirrors that fail-closed behavior.
    channel = _channel()
    seen = []
    channel.on_message(seen.append)
    c = _client(channel, _FakeVerifier(ok=False))
    r = c.post(
        "/herald/inbound/teams-bot",
        content=json.dumps(_MESSAGE).encode(),
        headers={"content-type": "application/json"},
    )
    assert r.status_code == 401
    assert seen == []


def test_bad_payload_400_after_valid_jwt():
    channel = _channel()
    c = _client(channel, _FakeVerifier(ok=True))
    r = c.post(
        "/herald/inbound/teams-bot",
        content=b"not-json",
        headers={"authorization": "Bearer good", "content-type": "application/json"},
    )
    assert r.status_code == 400


def test_botframework_verifier_rejects_missing_bearer():
    # The real verifier's fail-closed guard is exercisable without network:
    # no bearer token -> False before any JWKS fetch.
    from axiom.extensions.builtins.notifications.gateway.teams_bot import (
        BotFrameworkJwtVerifier,
    )

    v = BotFrameworkJwtVerifier(app_id="app-guid")
    assert v.verify(headers={}, body=b"{}") is False
    assert v.verify(headers={"authorization": "Basic xxx"}, body=b"{}") is False
