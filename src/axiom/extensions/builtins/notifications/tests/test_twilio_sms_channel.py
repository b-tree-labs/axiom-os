# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tests for ``TwilioSmsChannelAdapter`` — Twilio Programmable Messaging API.

Tier-A item #1 from the Radman connector blocker doc. Pins the quality
bar from the 2026-06-01 study:

- Capabilities (OUTBOUND + INTERNAL ceiling)
- ``build()`` requires (account_sid, auth_token, from_number)
- Twilio Messages API: POST /2010-04-01/Accounts/{Sid}/Messages.json
- Basic auth (Sid:Token); form-encoded body
- ``Retry-After`` parsed + honored on 429 / 503
- Exponential backoff with cap when no Retry-After
- ``ReconnectRequired`` on 401 / 403 (auth token revoked)
- ``retry_attempts`` populated in result
- Twilio does NOT support Idempotency-Key headers — adapter-level
  receipt-id dedup is the substitute (caller-supplied)
- Secret redaction (AuthToken + AccountSid never round-trip via errors)
- Twilio 11xxx error codes parsed into typed result fields where useful
"""

from __future__ import annotations

import pytest

from axiom.extensions.builtins.notifications.channels.base import Direction
from axiom.extensions.builtins.notifications.channels.twilio_sms import (
    TwilioSmsChannelAdapter,
    TwilioSmsChannelAdapterProvider,
)
from axiom.governance import Classification

ACCOUNT_SID = "AC11111111111111111111111111111111"
AUTH_TOKEN = "supersecretauthtoken12345abcdef"
FROM_NUM = "+15125550100"


class _FakePoster:
    """Captures every call; ``responses`` consumed in order to test retries."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []

    def post(self, url, data, auth, headers, timeout):
        self.calls.append({
            "url": url, "data": data, "auth": auth,
            "headers": headers, "timeout": timeout,
        })
        item = self._responses.pop(0)
        if isinstance(item, Exception):
            raise item
        status, body, hdrs = item
        return _FakeResp(status, body, hdrs)


class _FakeResp:
    def __init__(self, status_code, body, headers):
        self.status_code = status_code
        self.text = body if isinstance(body, str) else str(body)
        self._body = body
        self.headers = headers or {}

    def json(self):
        if isinstance(self._body, dict):
            return self._body
        import json
        return json.loads(self._body)


class _RecordingSleeper:
    def __init__(self):
        self.calls: list[float] = []

    def __call__(self, secs: float) -> None:
        self.calls.append(secs)


def _adapter(responses, sleeper=None):
    return TwilioSmsChannelAdapter(
        account_sid=ACCOUNT_SID,
        auth_token=AUTH_TOKEN,
        from_number=FROM_NUM,
        poster=_FakePoster(responses),
        sleeper=sleeper or _RecordingSleeper(),
    )


# ---------------------------------------------------------------------------
# Provider — capabilities + build
# ---------------------------------------------------------------------------


class TestTwilioProvider:
    def test_name(self):
        assert TwilioSmsChannelAdapterProvider().name == "twilio-sms"

    def test_capabilities_shape(self):
        caps = TwilioSmsChannelAdapterProvider().capabilities()
        assert caps.direction == Direction.OUTBOUND
        assert caps.classification_ceiling == Classification.INTERNAL
        assert caps.priority_levels == ("low", "normal", "high", "urgent")
        # SMS doesn't support threading the way chat channels do (each
        # message is independent until conversation-id wrapping lands).
        assert caps.supports_threading is False
        assert caps.supports_acknowledge is False
        assert caps.connector_ref == "twilio-account"

    def test_build_requires_all_three_fields(self):
        builder = TwilioSmsChannelAdapterProvider()
        with pytest.raises(ValueError, match="account_sid"):
            builder.build({"auth_token": "x", "from_number": "+1"})
        with pytest.raises(ValueError, match="auth_token"):
            builder.build({"account_sid": "AC", "from_number": "+1"})
        with pytest.raises(ValueError, match="from_number"):
            builder.build({"account_sid": "AC", "auth_token": "x"})

    def test_build_returns_adapter(self):
        adapter = TwilioSmsChannelAdapterProvider().build({
            "account_sid": ACCOUNT_SID,
            "auth_token": AUTH_TOKEN,
            "from_number": FROM_NUM,
        })
        assert adapter.name == "twilio-sms"


# ---------------------------------------------------------------------------
# Wire shape — endpoint + auth + body
# ---------------------------------------------------------------------------


class TestTwilioWireShape:
    def test_endpoint_uses_account_sid(self):
        adapter = _adapter([(201, {"sid": "SM123", "status": "queued"}, {})])
        adapter.deliver_sync(
            recipient="+15125550199",
            receipt_id="r-1",
            classification=Classification.INTERNAL,
            priority="normal",
            summary="hi",
        )
        url = adapter._poster.calls[0]["url"]  # type: ignore[attr-defined]
        assert ACCOUNT_SID in url
        assert "/Messages.json" in url
        assert url.startswith("https://api.twilio.com/2010-04-01/Accounts/")

    def test_basic_auth_sent(self):
        adapter = _adapter([(201, {"sid": "SM123"}, {})])
        adapter.deliver_sync(
            recipient="+15125550199",
            receipt_id="r-1",
            classification=Classification.INTERNAL,
            priority="normal",
            summary="hi",
        )
        auth = adapter._poster.calls[0]["auth"]  # type: ignore[attr-defined]
        assert auth == (ACCOUNT_SID, AUTH_TOKEN)

    def test_form_body_shape(self):
        adapter = _adapter([(201, {"sid": "SM123"}, {})])
        adapter.deliver_sync(
            recipient="+15125550199",
            receipt_id="r-1",
            classification=Classification.INTERNAL,
            priority="normal",
            summary="dose-rate excursion",
        )
        data = adapter._poster.calls[0]["data"]  # type: ignore[attr-defined]
        assert data["To"] == "+15125550199"
        assert data["From"] == FROM_NUM
        # Body includes the summary.
        assert "dose-rate excursion" in data["Body"]

    def test_returns_twilio_message_sid_on_success(self):
        adapter = _adapter([(201, {"sid": "SM987654321", "status": "queued"}, {})])
        result = adapter.deliver_sync(
            recipient="+15125550199",
            receipt_id="r-1",
            classification=Classification.INTERNAL,
            priority="normal",
            summary="hi",
        )
        assert result.ok is True
        assert result.message_sid == "SM987654321"
        assert result.status_code == 201


# ---------------------------------------------------------------------------
# Priority surfacing — SMS is short; urgency goes in the prefix
# ---------------------------------------------------------------------------


class TestPrioritySurfacing:
    def test_urgent_priority_prefix(self):
        adapter = _adapter([(201, {"sid": "x"}, {})])
        adapter.deliver_sync(
            recipient="+15125550199",
            receipt_id="r-1",
            classification=Classification.INTERNAL,
            priority="urgent",
            summary="dose excursion",
        )
        body = adapter._poster.calls[0]["data"]["Body"]  # type: ignore[attr-defined]
        # Urgent must visibly mark SMS — the operator may be reading on
        # a lock screen.
        assert any(m in body for m in ("URGENT", "🚨"))

    def test_normal_priority_no_prefix(self):
        adapter = _adapter([(201, {"sid": "x"}, {})])
        adapter.deliver_sync(
            recipient="+15125550199",
            receipt_id="r-1",
            classification=Classification.INTERNAL,
            priority="normal",
            summary="status update",
        )
        body = adapter._poster.calls[0]["data"]["Body"]  # type: ignore[attr-defined]
        assert "URGENT" not in body
        assert "status update" in body


# ---------------------------------------------------------------------------
# Reconnect-required — 401/403
# ---------------------------------------------------------------------------


class TestReconnectRequired:
    @pytest.mark.parametrize("status", [401, 403])
    def test_auth_class_returns_reconnect_required(self, status):
        sleeper = _RecordingSleeper()
        adapter = _adapter(
            [(status, {"code": 20003, "message": "Authenticate"}, {})],
            sleeper=sleeper,
        )
        result = adapter.deliver_sync(
            recipient="+15125550199", receipt_id="r-1",
            classification=Classification.INTERNAL,
            priority="normal", summary="hi",
        )
        assert result.ok is False
        assert result.reconnect_required is True
        assert result.status_code == status
        # No retries on auth-class.
        assert sleeper.calls == []


# ---------------------------------------------------------------------------
# Retry-After + backoff
# ---------------------------------------------------------------------------


class TestRetryBackoff:
    def test_429_with_retry_after_honored(self):
        sleeper = _RecordingSleeper()
        adapter = _adapter([
            (429, {"code": 20429, "message": "Too Many Requests"},
             {"Retry-After": "3"}),
            (201, {"sid": "SM-ok"}, {}),
        ], sleeper=sleeper)
        result = adapter.deliver_sync(
            recipient="+15125550199", receipt_id="r-1",
            classification=Classification.INTERNAL,
            priority="normal", summary="hi",
        )
        assert result.ok is True
        assert result.retry_attempts == 1
        assert sleeper.calls == [3.0]

    def test_503_without_retry_after_exponential(self):
        sleeper = _RecordingSleeper()
        adapter = _adapter([
            (503, {"message": "service unavailable"}, {}),
            (201, {"sid": "SM-ok"}, {}),
        ], sleeper=sleeper)
        adapter.deliver_sync(
            recipient="+15125550199", receipt_id="r-1",
            classification=Classification.INTERNAL,
            priority="normal", summary="hi",
        )
        assert sleeper.calls == [1.0]

    def test_max_attempts_then_give_up(self):
        sleeper = _RecordingSleeper()
        adapter = _adapter([
            (503, {"message": "down"}, {}),
            (503, {"message": "down"}, {}),
            (503, {"message": "down"}, {}),
        ], sleeper=sleeper)
        result = adapter.deliver_sync(
            recipient="+15125550199", receipt_id="r-1",
            classification=Classification.INTERNAL,
            priority="normal", summary="hi",
        )
        assert result.ok is False
        assert result.retry_attempts == 2
        assert result.status_code == 503

    def test_network_exception_retried(self):
        sleeper = _RecordingSleeper()
        adapter = _adapter([
            ConnectionError("dns"),
            (201, {"sid": "SM-ok"}, {}),
        ], sleeper=sleeper)
        result = adapter.deliver_sync(
            recipient="+15125550199", receipt_id="r-1",
            classification=Classification.INTERNAL,
            priority="normal", summary="hi",
        )
        assert result.ok is True


# ---------------------------------------------------------------------------
# Secret redaction — AuthToken + AccountSid
# ---------------------------------------------------------------------------


class TestSecretRedaction:
    def test_auth_token_never_in_error(self):
        adapter = _adapter([
            (500, f"failed; token was {AUTH_TOKEN}", {}),
        ])
        result = adapter.deliver_sync(
            recipient="+15125550199", receipt_id="r-1",
            classification=Classification.INTERNAL,
            priority="normal", summary="hi",
        )
        assert AUTH_TOKEN not in (result.error or "")

    def test_account_sid_redacted_in_error(self):
        adapter = _adapter([
            (500, f"see account {ACCOUNT_SID}", {}),
        ])
        result = adapter.deliver_sync(
            recipient="+15125550199", receipt_id="r-1",
            classification=Classification.INTERNAL,
            priority="normal", summary="hi",
        )
        # AccountSid is sensitive — leaks identify the customer.
        assert ACCOUNT_SID not in (result.error or "")


# ---------------------------------------------------------------------------
# Twilio error-code surfacing
# ---------------------------------------------------------------------------


class TestErrorCodeSurfacing:
    def test_twilio_error_code_in_result(self):
        # Common Twilio errors: 21211 invalid To number, 21610 unsubscribed.
        adapter = _adapter([
            (400, {"code": 21211, "message": "Invalid 'To' Phone Number"}, {}),
        ])
        result = adapter.deliver_sync(
            recipient="bogus", receipt_id="r-1",
            classification=Classification.INTERNAL,
            priority="normal", summary="hi",
        )
        assert result.ok is False
        # The Twilio code is surfaced for operator triage.
        assert result.twilio_code == 21211
