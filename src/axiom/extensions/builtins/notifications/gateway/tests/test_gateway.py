# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0

"""HERALD Gateway scaffold tests (ADR-067 PR-1).

Pins the inbound contract: unknown vendor → 404, bad signature → 401,
accepted → 202 + one bus event, vendor retry → 200 duplicate (no second
event), dedup TTL expiry, and the HMAC verifier.
"""

from __future__ import annotations

import hashlib
import hmac

from fastapi import FastAPI
from fastapi.testclient import TestClient

from axiom.extensions.builtins.notifications.gateway import (
    AllowAllVerifier,
    DedupCache,
    HmacSha256Verifier,
    VerifierRegistry,
    build_gateway_router,
)


class _FakeBus:
    def __init__(self):
        self.events = []

    def publish(self, subject, payload=None, source=""):
        self.events.append({"subject": subject, "payload": payload, "source": source})
        return None


def _client(verifiers, bus, dedup=None):
    app = FastAPI()
    app.include_router(build_gateway_router(bus=bus, verifiers=verifiers, dedup=dedup))
    return TestClient(app)


def _verifiers(vendor="slack", verifier=None):
    reg = VerifierRegistry()
    reg.register(vendor, verifier or AllowAllVerifier())
    return reg


# --- routing + verification ----------------------------------------------- #
def test_unknown_vendor_404():
    bus = _FakeBus()
    c = _client(VerifierRegistry(), bus)
    r = c.post("/herald/inbound/slack", json={"event_id": "e1"})
    assert r.status_code == 404
    assert not bus.events


def test_accepted_publishes_one_event():
    bus = _FakeBus()
    c = _client(_verifiers(), bus)
    r = c.post("/herald/inbound/slack", json={"event_id": "e1", "text": "hi", "user": "U1"})
    assert r.status_code == 202
    assert r.json()["event_id"] == "e1"
    assert len(bus.events) == 1
    ev = bus.events[0]
    assert ev["subject"] == "herald.inbound.slack"
    assert ev["payload"]["text"] == "hi"
    assert ev["payload"]["sender_ref"] == "U1"
    assert ev["source"] == "herald.gateway"


def test_duplicate_event_not_republished():
    bus = _FakeBus()
    c = _client(_verifiers(), bus)
    first = c.post("/herald/inbound/slack", json={"event_id": "e1"})
    second = c.post("/herald/inbound/slack", json={"event_id": "e1"})
    assert first.status_code == 202
    assert second.status_code == 200
    assert second.json()["status"] == "duplicate"
    assert len(bus.events) == 1  # only the first


def test_bad_payload_400():
    bus = _FakeBus()
    c = _client(_verifiers(), bus)
    r = c.post(
        "/herald/inbound/slack",
        content=b"not-json",
        headers={"content-type": "application/json"},
    )
    assert r.status_code == 400
    assert not bus.events


# --- HMAC verifier --------------------------------------------------------- #
def test_hmac_verifier_rejects_bad_signature():
    bus = _FakeBus()
    v = HmacSha256Verifier("s3cret", header="x-sig")
    c = _client(_verifiers(verifier=v), bus)
    r = c.post("/herald/inbound/slack", content=b"{}", headers={"x-sig": "deadbeef"})
    assert r.status_code == 401
    assert not bus.events


def test_hmac_verifier_accepts_good_signature():
    bus = _FakeBus()
    secret = "s3cret"
    v = HmacSha256Verifier(secret, header="x-sig")
    c = _client(_verifiers(verifier=v), bus)
    body = b'{"event_id": "e9"}'
    sig = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    r = c.post(
        "/herald/inbound/slack",
        content=body,
        headers={"x-sig": sig, "content-type": "application/json"},
    )
    assert r.status_code == 202
    assert len(bus.events) == 1


# --- dedup unit ------------------------------------------------------------ #
def test_dedup_ttl_expiry():
    t = {"now": 1000.0}
    cache = DedupCache(ttl_seconds=10, clock=lambda: t["now"])
    assert cache.seen_or_add("slack", "e1") is False
    assert cache.seen_or_add("slack", "e1") is True
    t["now"] += 11  # past TTL
    assert cache.seen_or_add("slack", "e1") is False  # expired → new again


def test_dedup_blank_event_id_never_dedupes():
    cache = DedupCache()
    assert cache.seen_or_add("slack", "") is False
    assert cache.seen_or_add("slack", "") is False


def test_dedup_is_per_vendor():
    cache = DedupCache()
    assert cache.seen_or_add("slack", "e1") is False
    assert cache.seen_or_add("teams", "e1") is False  # same id, different vendor


# --- mounts on the real http create_app ----------------------------------- #
def test_mount_gateway_on_http_create_app():
    from axiom.extensions.builtins.http.server import create_app
    from axiom.extensions.builtins.notifications.gateway import mount_gateway

    bus = _FakeBus()
    app = create_app(title="t")
    mount_gateway(app, bus=bus, verifiers=_verifiers())
    c = TestClient(app)
    r = c.post("/herald/inbound/slack", json={"event_id": "e1", "text": "hi"})
    assert r.status_code == 202
    assert bus.events[0]["subject"] == "herald.inbound.slack"
