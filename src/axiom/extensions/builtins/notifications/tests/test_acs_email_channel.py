# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0

"""Tests for the Azure Communication Services email backend (``acs``)."""

from __future__ import annotations

import base64
import json

from axiom.extensions.builtins.notifications.channels.email import (
    AcsEmailProvider,
    EmailChannelAdapterProvider,
    EmailMessage,
    detect_email_provider,
    email_provider_names,
)
from axiom.governance import Classification

ACCESS_KEY = base64.b64encode(b"secret-acs-email-key").decode()
ENDPOINT = "https://res.communication.azure.com"
CONNECTION_STRING = f"endpoint={ENDPOINT}/;accesskey={ACCESS_KEY}"


class _FakeResp:
    def __init__(self, status_code, body, headers=None):
        self.status_code = status_code
        self._body = body
        self.text = body if isinstance(body, str) else json.dumps(body)
        self.headers = headers or {}

    def json(self):
        if isinstance(self._body, dict):
            return self._body
        return json.loads(self._body)


class _FakePoster:
    def __init__(self, status_code=202, body=None, headers=None, raise_exc=None):
        self.status_code = status_code
        self.body = body if body is not None else {"id": "acs-op-1"}
        self.headers = headers or {}
        self.raise_exc = raise_exc
        self.calls: list[dict] = []

    def post(self, url, content, headers, timeout):
        self.calls.append(
            {"url": url, "content": content, "headers": headers, "timeout": timeout}
        )
        if self.raise_exc is not None:
            raise self.raise_exc
        return _FakeResp(self.status_code, self.body, self.headers)


def _msg(**overrides):
    base = dict(
        to=("alice@example.com",),
        subject="ingest complete",
        from_address="herald@verified.example.com",
        body_text="done",
    )
    base.update(overrides)
    return EmailMessage(**base)


def test_registered_at_import():
    assert "acs" in email_provider_names()


def test_detect_via_connection_string():
    p = detect_email_provider({"acs_connection_string": CONNECTION_STRING})
    assert isinstance(p, AcsEmailProvider)
    assert p.name == "acs"


def test_happy_path_hmac():
    poster = _FakePoster(status_code=202, body={"id": "acs-op-9"})
    p = AcsEmailProvider(endpoint=ENDPOINT, access_key=ACCESS_KEY, poster=poster)
    result = p.send(_msg(body_html="<b>done</b>"))
    assert result.ok is True
    assert result.provider == "acs"
    assert result.message_id == "acs-op-9"
    call = poster.calls[0]
    assert "/emails:send?api-version=" in call["url"]
    payload = json.loads(call["content"])
    assert payload["senderAddress"] == "herald@verified.example.com"
    assert payload["content"]["subject"] == "ingest complete"
    assert payload["content"]["plainText"] == "done"
    assert payload["content"]["html"] == "<b>done</b>"
    assert payload["recipients"]["to"] == [{"address": "alice@example.com"}]
    assert call["headers"]["Authorization"].startswith("HMAC-SHA256 ")


def test_entra_bearer_path():
    poster = _FakePoster()
    p = AcsEmailProvider(
        endpoint=ENDPOINT, access_token="entra-abc", poster=poster
    )
    p.send(_msg())
    assert poster.calls[0]["headers"]["Authorization"] == "Bearer entra-abc"


def test_4xx_is_failure_and_redacts_key():
    poster = _FakePoster(status_code=401, body=f"denied {ACCESS_KEY}")
    p = AcsEmailProvider(endpoint=ENDPOINT, access_key=ACCESS_KEY, poster=poster)
    result = p.send(_msg())
    assert result.ok is False
    assert result.status_code == 401
    assert ACCESS_KEY not in (result.error or "")


def test_via_email_channel_adapter():
    poster = _FakePoster(status_code=202, body={"id": "acs-chan"})
    adapter = EmailChannelAdapterProvider().build(
        {
            "from_address": "herald@verified.example.com",
            "acs_connection_string": CONNECTION_STRING,
            "poster": poster,
        }
    )
    result = adapter.deliver_sync(
        recipient="alice@example.com",
        receipt_id="r-1",
        classification=Classification.INTERNAL,
        priority="normal",
        summary="hello",
    )
    assert result.ok is True
    assert result.provider == "acs"
