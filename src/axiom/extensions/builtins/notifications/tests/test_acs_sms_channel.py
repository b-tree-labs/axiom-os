# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0

"""Tests for ``AcsSmsChannelAdapter`` — Azure Communication Services SMS.

Pins:
- Capabilities (OUTBOUND + INTERNAL ceiling)
- build requires ``from_number`` + (``connection_string`` or ``endpoint``)
- REST wire shape (``/sms?api-version=...``, from/smsRecipients/message)
- HMAC ``Authorization`` header (connection-string auth) + Entra bearer path
- ``Retry-After`` honored on 429; reconnect_required on 401/403
- access key never round-trips through error text
"""

from __future__ import annotations

import base64
import json

import pytest

from axiom.extensions.builtins.notifications.channels.acs_sms import (
    AcsSmsChannelAdapter,
    AcsSmsChannelAdapterProvider,
)
from axiom.extensions.builtins.notifications.channels.base import Direction
from axiom.governance import Classification

ACCESS_KEY = base64.b64encode(b"super-secret-acs-key-material").decode()
ENDPOINT = "https://res.communication.azure.com"
CONNECTION_STRING = f"endpoint={ENDPOINT}/;accesskey={ACCESS_KEY}"
FROM_NUM = "+15125550100"


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
    def __init__(self, responses):
        self._responses = list(responses)
        self.calls: list[dict] = []

    def post(self, url, content, headers, timeout):
        self.calls.append(
            {"url": url, "content": content, "headers": headers, "timeout": timeout}
        )
        item = self._responses.pop(0)
        if isinstance(item, Exception):
            raise item
        status, body, hdrs = item
        return _FakeResp(status, body, hdrs)


class _Sleeper:
    def __init__(self):
        self.calls: list[float] = []

    def __call__(self, secs):
        self.calls.append(secs)


_OK_BODY = {"value": [{"to": "+1", "messageId": "acs-msg-1", "httpStatusCode": 202}]}


def _hmac_adapter(responses, sleeper=None):
    return AcsSmsChannelAdapter(
        endpoint=ENDPOINT,
        from_number=FROM_NUM,
        access_key=ACCESS_KEY,
        poster=_FakePoster(responses),
        sleeper=sleeper or _Sleeper(),
    )


class TestProvider:
    def test_capabilities(self):
        caps = AcsSmsChannelAdapterProvider().capabilities()
        assert caps.direction == Direction.OUTBOUND
        assert caps.classification_ceiling == Classification.INTERNAL

    def test_build_requires_from_number(self):
        with pytest.raises(ValueError, match="from_number"):
            AcsSmsChannelAdapterProvider().build(
                {"connection_string": CONNECTION_STRING}
            )

    def test_build_requires_auth_source(self):
        with pytest.raises(ValueError, match="connection_string|endpoint"):
            AcsSmsChannelAdapterProvider().build({"from_number": FROM_NUM})

    def test_build_from_connection_string(self):
        adapter = AcsSmsChannelAdapterProvider().build(
            {"from_number": FROM_NUM, "connection_string": CONNECTION_STRING}
        )
        assert adapter.name == "acs-sms"


class TestWireShape:
    def test_happy_path(self):
        adapter = _hmac_adapter([(202, _OK_BODY, {})])
        result = adapter.deliver_sync(
            recipient="+15125550199",
            receipt_id="r-1",
            classification=Classification.INTERNAL,
            priority="normal",
            summary="dose excursion",
        )
        assert result.ok is True
        assert result.message_id == "acs-msg-1"
        call = adapter._poster.calls[0]  # type: ignore[attr-defined]
        assert "/sms?api-version=" in call["url"]
        payload = json.loads(call["content"])
        assert payload["from"] == FROM_NUM
        assert payload["smsRecipients"] == [{"to": "+15125550199"}]
        assert "dose excursion" in payload["message"]
        # HMAC auth headers present.
        assert call["headers"]["Authorization"].startswith("HMAC-SHA256 ")
        assert "x-ms-content-sha256" in call["headers"]

    def test_entra_bearer_path(self):
        adapter = AcsSmsChannelAdapter(
            endpoint=ENDPOINT,
            from_number=FROM_NUM,
            access_token="entra-token-xyz",
            poster=_FakePoster([(202, _OK_BODY, {})]),
        )
        adapter.deliver_sync(
            recipient="+15125550199",
            receipt_id="r-1",
            classification=Classification.INTERNAL,
            priority="normal",
            summary="hi",
        )
        auth = adapter._poster.calls[0]["headers"]["Authorization"]  # type: ignore[attr-defined]
        assert auth == "Bearer entra-token-xyz"

    def test_urgent_prefix(self):
        adapter = _hmac_adapter([(202, _OK_BODY, {})])
        adapter.deliver_sync(
            recipient="+15125550199",
            receipt_id="r-1",
            classification=Classification.INTERNAL,
            priority="urgent",
            summary="scram",
        )
        payload = json.loads(adapter._poster.calls[0]["content"])  # type: ignore[attr-defined]
        assert any(m in payload["message"] for m in ("URGENT", "🚨"))


class TestRetryAndAuth:
    @pytest.mark.parametrize("status", [401, 403])
    def test_reconnect_on_auth(self, status):
        sleeper = _Sleeper()
        adapter = _hmac_adapter([(status, "denied", {})], sleeper=sleeper)
        result = adapter.deliver_sync(
            recipient="+15125550199",
            receipt_id="r-1",
            classification=Classification.INTERNAL,
            priority="normal",
            summary="hi",
        )
        assert result.ok is False
        assert result.reconnect_required is True
        assert sleeper.calls == []

    def test_429_retry_after_honored(self):
        sleeper = _Sleeper()
        adapter = _hmac_adapter(
            [(429, "slow down", {"Retry-After": "2"}), (202, _OK_BODY, {})],
            sleeper=sleeper,
        )
        result = adapter.deliver_sync(
            recipient="+15125550199",
            receipt_id="r-1",
            classification=Classification.INTERNAL,
            priority="normal",
            summary="hi",
        )
        assert result.ok is True
        assert result.retry_attempts == 1
        assert sleeper.calls == [2.0]

    def test_access_key_redacted(self):
        # 400 is non-retryable so a single response returns immediately.
        adapter = _hmac_adapter([(400, f"boom {ACCESS_KEY}", {})])
        result = adapter.deliver_sync(
            recipient="+15125550199",
            receipt_id="r-1",
            classification=Classification.INTERNAL,
            priority="normal",
            summary="hi",
        )
        assert result.ok is False
        assert ACCESS_KEY not in (result.error or "")
