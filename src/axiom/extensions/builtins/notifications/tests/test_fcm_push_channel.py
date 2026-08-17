# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0

"""Tests for ``FcmPushChannelAdapter`` — Firebase Cloud Messaging HTTP v1."""

from __future__ import annotations

import pytest

from axiom.extensions.builtins.notifications.channels.base import Direction
from axiom.extensions.builtins.notifications.channels.fcm_push import (
    FcmPushChannelAdapter,
    FcmPushChannelAdapterProvider,
)
from axiom.governance import Classification

TOKEN = "ya29.a0AfH-fcm-bearer-secret"
PROJECT = "my-fcm-project"


class _FakeResp:
    def __init__(self, status_code, body):
        self.status_code = status_code
        self._body = body
        self.text = str(body)

    def json(self):
        return self._body


class _FakePoster:
    def __init__(self, status_code=200, body=None, raise_exc=None):
        self.status_code = status_code
        self.body = body if body is not None else {"name": "projects/x/messages/1"}
        self.raise_exc = raise_exc
        self.calls: list[dict] = []

    def post(self, url, json, headers, timeout):
        self.calls.append({"url": url, "json": json, "headers": headers})
        if self.raise_exc is not None:
            raise self.raise_exc
        return _FakeResp(self.status_code, self.body)


def _adapter(poster):
    return FcmPushChannelAdapter(
        project_id=PROJECT, access_token=TOKEN, poster=poster
    )


class TestProvider:
    def test_capabilities(self):
        caps = FcmPushChannelAdapterProvider().capabilities()
        assert caps.direction == Direction.OUTBOUND
        assert caps.classification_ceiling == Classification.INTERNAL

    def test_build_requires_project_and_token(self):
        with pytest.raises(ValueError, match="project_id"):
            FcmPushChannelAdapterProvider().build({"access_token": TOKEN})
        with pytest.raises(ValueError, match="access_token"):
            FcmPushChannelAdapterProvider().build({"project_id": PROJECT})


class TestWireShape:
    def test_device_token_recipient(self):
        poster = _FakePoster(body={"name": "projects/x/messages/42"})
        result = _adapter(poster).deliver_sync(
            recipient="device-token-abc",
            receipt_id="r-1",
            classification=Classification.INTERNAL,
            priority="urgent",
            summary="scram",
        )
        assert result.ok is True
        assert result.message_id == "projects/x/messages/42"
        call = poster.calls[0]
        assert PROJECT in call["url"]
        assert call["headers"]["Authorization"] == f"Bearer {TOKEN}"
        msg = call["json"]["message"]
        assert msg["token"] == "device-token-abc"
        assert msg["notification"]["body"] == "scram"
        assert "URGENT" in msg["notification"]["title"]

    def test_topic_recipient(self):
        poster = _FakePoster()
        _adapter(poster).deliver_sync(
            recipient="/topics/reactor-alerts",
            receipt_id="r-1",
            classification=Classification.INTERNAL,
            priority="normal",
            summary="hi",
        )
        msg = poster.calls[0]["json"]["message"]
        assert msg["topic"] == "reactor-alerts"
        assert "token" not in msg


class TestFailures:
    def test_401_sets_reconnect(self):
        poster = _FakePoster(status_code=401, body={"error": "unauth"})
        result = _adapter(poster).deliver_sync(
            recipient="device",
            receipt_id="r-1",
            classification=Classification.INTERNAL,
            priority="normal",
            summary="hi",
        )
        assert result.ok is False
        assert result.reconnect_required is True

    def test_token_redacted(self):
        poster = _FakePoster(status_code=500, body=f"leaked {TOKEN}")
        result = _adapter(poster).deliver_sync(
            recipient="device",
            receipt_id="r-1",
            classification=Classification.INTERNAL,
            priority="normal",
            summary="hi",
        )
        assert result.ok is False
        assert TOKEN not in (result.error or "")
