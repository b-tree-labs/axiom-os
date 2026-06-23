# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0

"""Slack inbound: signing verifier + Events API decode + URL handshake (PR-4)."""

from __future__ import annotations

import hashlib
import hmac
import json

from fastapi import FastAPI
from fastapi.testclient import TestClient

from axiom.extensions.builtins.notifications.gateway import (
    SlackDecoder,
    SlackSigningVerifier,
    VerifierRegistry,
    build_gateway_router,
)
from axiom.extensions.builtins.notifications.gateway.decode import DecoderRegistry

SECRET = "8f742231b10e8888abcd99yyyzzz85a5"


def _sign(secret: str, ts: str, body: bytes) -> str:
    base = b"v0:" + ts.encode() + b":" + body
    return "v0=" + hmac.new(secret.encode(), base, hashlib.sha256).hexdigest()


class _FakeBus:
    def __init__(self):
        self.events = []

    def publish(self, subject, payload=None, source=""):
        self.events.append({"subject": subject, "payload": payload})


# --- signing verifier ------------------------------------------------------ #
def test_verifier_accepts_valid_signature():
    v = SlackSigningVerifier(SECRET, clock=lambda: 1000.0)
    body = b'{"x":1}'
    headers = {
        "x-slack-request-timestamp": "1000",
        "x-slack-signature": _sign(SECRET, "1000", body),
    }
    assert v.verify(headers=headers, body=body) is True


def test_verifier_rejects_bad_signature():
    v = SlackSigningVerifier(SECRET, clock=lambda: 1000.0)
    headers = {
        "x-slack-request-timestamp": "1000",
        "x-slack-signature": "v0=deadbeef",
    }
    assert v.verify(headers=headers, body=b"{}") is False


def test_verifier_rejects_stale_timestamp():
    v = SlackSigningVerifier(SECRET, clock=lambda: 1000.0, max_skew_seconds=300)
    body = b"{}"
    headers = {
        "x-slack-request-timestamp": "1",  # ~1000s old
        "x-slack-signature": _sign(SECRET, "1", body),
    }
    assert v.verify(headers=headers, body=body) is False


def test_verifier_rejects_missing_headers():
    v = SlackSigningVerifier(SECRET)
    assert v.verify(headers={}, body=b"{}") is False


# --- decoder --------------------------------------------------------------- #
def test_decoder_normalizes_app_mention():
    body = {
        "event_id": "Ev123",
        "event": {
            "type": "app_mention",
            "text": "<@U0BOT> rebase PR 12",
            "user": "U0HUMAN",
            "ts": "1700000000.000100",
        },
    }
    ev = SlackDecoder().decode("slack", body)
    assert ev.event_id == "Ev123"
    assert ev.text == "<@U0BOT> rebase PR 12"
    assert ev.sender_ref == "U0HUMAN"
    assert ev.thread_ref == "1700000000.000100"


# --- route: URL verification handshake ------------------------------------- #
def _client(bus, *, secret=SECRET, clock=lambda: 1000.0):
    verifiers = VerifierRegistry()
    verifiers.register("slack", SlackSigningVerifier(secret, clock=clock))
    decoders = DecoderRegistry()
    decoders.register("slack", SlackDecoder())
    app = FastAPI()
    app.include_router(
        build_gateway_router(bus=bus, verifiers=verifiers, decoders=decoders)
    )
    return TestClient(app)


def test_url_verification_echoes_challenge_no_publish():
    bus = _FakeBus()
    c = _client(bus)
    body = json.dumps({"type": "url_verification", "challenge": "abc123"}).encode()
    headers = {
        "x-slack-request-timestamp": "1000",
        "x-slack-signature": _sign(SECRET, "1000", body),
        "content-type": "application/json",
    }
    r = c.post("/herald/inbound/slack", content=body, headers=headers)
    assert r.status_code == 200
    assert r.json()["challenge"] == "abc123"
    assert not bus.events  # handshake never publishes


# --- route: signed app_mention end to end ---------------------------------- #
def test_signed_app_mention_publishes_event():
    bus = _FakeBus()
    c = _client(bus)
    body = json.dumps(
        {
            "event_id": "Ev999",
            "event": {"type": "app_mention", "text": "@rivet status", "user": "U1"},
        }
    ).encode()
    headers = {
        "x-slack-request-timestamp": "1000",
        "x-slack-signature": _sign(SECRET, "1000", body),
        "content-type": "application/json",
    }
    r = c.post("/herald/inbound/slack", content=body, headers=headers)
    assert r.status_code == 202
    assert len(bus.events) == 1
    assert bus.events[0]["subject"] == "herald.inbound.slack"
    assert bus.events[0]["payload"]["text"] == "@rivet status"


def test_unsigned_request_rejected_401():
    bus = _FakeBus()
    c = _client(bus)
    r = c.post("/herald/inbound/slack", json={"event_id": "x"})
    assert r.status_code == 401
    assert not bus.events


def test_decoder_captures_channel_for_reply():
    # The reply path needs the Slack channel; it must survive decode → payload.
    body = {
        "event_id": "Ev1",
        "event": {"type": "app_mention", "text": "@rivet hi", "user": "U1",
                  "channel": "C0ROOM", "ts": "1.2"},
    }
    ev = SlackDecoder().decode("slack", body)
    assert ev.channel == "C0ROOM"
    assert ev.as_payload()["channel"] == "C0ROOM"


def test_bot_authored_event_is_ignored_loop_guard():
    # The bot's own reply must NOT be re-ingested (infinite-loop guard).
    dec = SlackDecoder()
    assert dec.ignore("slack", {"event": {"bot_id": "B123", "text": "hi"}}) is True
    assert dec.ignore("slack", {"event": {"subtype": "bot_message"}}) is True
    assert dec.ignore("slack", {"event": {"user": "U1", "text": "@rivet go"}}) is False


def test_route_drops_bot_authored_event(tmp_path=None):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from axiom.extensions.builtins.notifications.gateway import (
        VerifierRegistry,
        build_gateway_router,
    )
    from axiom.extensions.builtins.notifications.gateway.decode import DecoderRegistry

    bus = _FakeBus()
    secret = SECRET
    verifiers = VerifierRegistry()
    verifiers.register("slack", SlackSigningVerifier(secret, clock=lambda: 1000.0))
    decoders = DecoderRegistry()
    decoders.register("slack", SlackDecoder())
    app = FastAPI()
    app.include_router(
        build_gateway_router(bus=bus, verifiers=verifiers, decoders=decoders)
    )
    c = TestClient(app)
    body = json.dumps(
        {"event_id": "Evbot", "event": {"bot_id": "B1", "text": "@rivet loop"}}
    ).encode()
    headers = {
        "x-slack-request-timestamp": "1000",
        "x-slack-signature": _sign(secret, "1000", body),
        "content-type": "application/json",
    }
    r = c.post("/herald/inbound/slack", content=body, headers=headers)
    assert r.status_code == 200
    assert r.json()["status"] == "ignored"
    assert not bus.events  # never published → never dispatched → no loop
