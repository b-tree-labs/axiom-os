# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0

"""Tests for the Gmail API email backend (``gmail``)."""

from __future__ import annotations

import base64

import pytest

from axiom.extensions.builtins.notifications.channels.email import (
    EmailChannelAdapterProvider,
    EmailMessage,
    GmailEmailProvider,
    detect_email_provider,
    email_provider_names,
)
from axiom.governance import Classification

ACCESS_TOKEN = "ya29.a0AfH-supersecrettoken"


class _FakeResp:
    def __init__(self, status_code, body):
        self.status_code = status_code
        self._body = body
        self.text = str(body)

    def json(self):
        return self._body


class _FakePoster:
    """Handles both the send POST (json=) and the token refresh POST (data=)."""

    def __init__(self, responses):
        # responses: list of (status, body) consumed in order
        self._responses = list(responses)
        self.calls: list[dict] = []

    def post(self, url, timeout=10.0, json=None, data=None, headers=None):
        self.calls.append(
            {"url": url, "json": json, "data": data, "headers": headers}
        )
        status, body = self._responses.pop(0)
        return _FakeResp(status, body)


def _msg(**overrides):
    base = dict(
        to=("alice@example.com",),
        subject="ingest complete",
        from_address="herald@gmail.com",
        body_text="done",
    )
    base.update(overrides)
    return EmailMessage(**base)


def test_registered_at_import():
    assert "gmail" in email_provider_names()


def test_detect_via_access_token():
    p = detect_email_provider({"gmail_access_token": ACCESS_TOKEN})
    assert isinstance(p, GmailEmailProvider)


def test_build_requires_token_or_refresh_creds():
    with pytest.raises(ValueError, match="gmail"):
        GmailEmailProvider()


def test_happy_path_with_access_token():
    poster = _FakePoster([(200, {"id": "gmail-1", "threadId": "t1"})])
    p = GmailEmailProvider(access_token=ACCESS_TOKEN, poster=poster)
    result = p.send(_msg(from_name="HERALD"))
    assert result.ok is True
    assert result.message_id == "gmail-1"
    call = poster.calls[0]
    assert call["url"].endswith("/users/me/messages/send")
    assert call["headers"]["Authorization"] == f"Bearer {ACCESS_TOKEN}"
    # raw is base64url of the MIME message.
    raw = call["json"]["raw"]
    decoded = base64.urlsafe_b64decode(raw).decode("utf-8")
    assert "Subject: ingest complete" in decoded
    assert "alice@example.com" in decoded


def test_refresh_token_exchange_then_send():
    poster = _FakePoster(
        [
            (200, {"access_token": "ya29.minted", "expires_in": 3600}),
            (200, {"id": "gmail-2"}),
        ]
    )
    p = GmailEmailProvider(
        refresh_token="1//refresh",
        client_id="cid",
        client_secret="csecret",
        poster=poster,
    )
    result = p.send(_msg())
    assert result.ok is True
    # First call = token endpoint (data=), second = send (json=).
    assert poster.calls[0]["data"]["grant_type"] == "refresh_token"
    assert poster.calls[1]["headers"]["Authorization"] == "Bearer ya29.minted"


def test_4xx_is_failure():
    poster = _FakePoster([(403, {"error": "forbidden"})])
    p = GmailEmailProvider(access_token=ACCESS_TOKEN, poster=poster)
    result = p.send(_msg())
    assert result.ok is False
    assert result.status_code == 403


def test_access_token_redacted_in_error():
    poster = _FakePoster([(500, f"leaked {ACCESS_TOKEN}")])
    p = GmailEmailProvider(access_token=ACCESS_TOKEN, poster=poster)
    result = p.send(_msg())
    assert result.ok is False
    assert ACCESS_TOKEN not in (result.error or "")


def test_via_email_channel_adapter():
    poster = _FakePoster([(200, {"id": "gmail-chan"})])
    adapter = EmailChannelAdapterProvider().build(
        {
            "from_address": "herald@gmail.com",
            "gmail_access_token": ACCESS_TOKEN,
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
    assert result.provider == "gmail"
