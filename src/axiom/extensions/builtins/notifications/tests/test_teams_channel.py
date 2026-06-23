# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tests for ``TeamsChannelAdapter`` — Microsoft Teams Workflows webhook.

First connector built to the connector-quality bar from the 2026-06-01
study (§7). Pins:

- Provider capabilities (OUTBOUND + INTERNAL ceiling + SLA + connector_ref)
- ``build()`` requires the Workflows trigger URL
- Adaptive Card payload shape (attachment with adaptive-card content +
  FactSet rows for to/priority/classification/receipt)
- ``Idempotency-Key`` header included on every send; defaults derive from
  receipt-id so retry-duplication can't double-post
- ``Retry-After`` parsed + honored on 429 / 503
- Exponential backoff with cap when no Retry-After
- ``reconnect_required=True`` on 401 / 403 (no retries; auth-class)
- ``retry_attempts`` populated in the result on retry paths
- Secret-redaction on every error path (``sig=`` query + ``invoke/<id>``
  segment never round-trip through errors)
"""

from __future__ import annotations

import pytest

from axiom.extensions.builtins.notifications.channels.base import Direction
from axiom.extensions.builtins.notifications.channels.teams import (
    TeamsChannelAdapter,
    TeamsChannelAdapterProvider,
)
from axiom.governance import Classification

# Workflows-trigger URL with both redacted secret surfaces.
WEBHOOK = (
    "https://acme.logic.azure.com/workflows/abc/triggers/manual/paths/"
    "invoke/long-invoke-id-12345?api-version=2026-05-01&sig=SECRETSIGTOKEN"
)


# ---------------------------------------------------------------------------
# Stubs
# ---------------------------------------------------------------------------


class _FakePoster:
    """Captures every call; ``responses`` is a list of (status, body, headers)
    consumed in order so retry paths can be tested."""

    def __init__(self, responses):
        # Each element: (status_code, body, headers_dict) or an Exception.
        self._responses = list(responses)
        self.calls = []

    def post(self, url, json, headers, timeout):
        self.calls.append(
            {"url": url, "json": json, "headers": headers, "timeout": timeout}
        )
        item = self._responses.pop(0)
        if isinstance(item, Exception):
            raise item
        status, body, hdrs = item
        return _FakeResp(status, body, hdrs)


class _FakeResp:
    def __init__(self, status_code, text, headers):
        self.status_code = status_code
        self.text = text
        self.headers = headers or {}


class _RecordingSleeper:
    def __init__(self):
        self.calls: list[float] = []

    def __call__(self, secs: float) -> None:
        self.calls.append(secs)


def _adapter(responses, sleeper=None):
    return TeamsChannelAdapter(
        webhook_url=WEBHOOK,
        poster=_FakePoster(responses),
        sleeper=sleeper or _RecordingSleeper(),
    )


# ---------------------------------------------------------------------------
# Provider — capabilities + build
# ---------------------------------------------------------------------------


class TestTeamsProvider:
    def test_name(self):
        assert TeamsChannelAdapterProvider().name == "teams"

    def test_capabilities_shape(self):
        caps = TeamsChannelAdapterProvider().capabilities()
        assert caps.direction == Direction.OUTBOUND
        assert caps.classification_ceiling == Classification.INTERNAL
        assert caps.priority_levels == ("low", "normal", "high", "urgent")
        assert caps.supports_threading is True
        assert caps.supports_acknowledge is False
        assert caps.connector_ref == "teams-workflow-webhook"

    def test_build_requires_webhook_url(self):
        with pytest.raises(ValueError, match="webhook_url"):
            TeamsChannelAdapterProvider().build({})

    def test_build_accepts_workflows_url(self):
        adapter = TeamsChannelAdapterProvider().build({"webhook_url": WEBHOOK})
        assert adapter.name == "teams"


# ---------------------------------------------------------------------------
# Payload shape — Adaptive Card
# ---------------------------------------------------------------------------


class TestAdaptiveCardPayload:
    def test_payload_has_attachment_with_adaptive_card(self):
        poster_resp = [(200, "ok", {})]
        adapter = _adapter(poster_resp)
        adapter.deliver_sync(
            recipient="#general",
            receipt_id="r-1",
            classification=Classification.INTERNAL,
            priority="normal",
            summary="DP1 RAG ingest complete",
        )
        sent = adapter._poster.calls[0]["json"]  # type: ignore[attr-defined]
        assert sent["type"] == "message"
        att = sent["attachments"][0]
        assert (
            att["contentType"]
            == "application/vnd.microsoft.card.adaptive"
        )
        card = att["content"]
        assert card["type"] == "AdaptiveCard"
        # FactSet captures the metadata.
        fact_titles = {
            f["title"] for f in card["body"][1]["facts"]
        }
        assert {"to", "priority", "classification", "receipt"} <= fact_titles

    def test_urgent_priority_colors_card(self):
        poster_resp = [(200, "ok", {})]
        adapter = _adapter(poster_resp)
        adapter.deliver_sync(
            recipient="#general",
            receipt_id="r-1",
            classification=Classification.INTERNAL,
            priority="urgent",
            summary="trunk red",
        )
        card = adapter._poster.calls[0]["json"]["attachments"][0]["content"]  # type: ignore[attr-defined]
        # Title TextBlock should reflect attention color for urgent.
        assert card["body"][0]["color"] == "attention"


# ---------------------------------------------------------------------------
# Idempotency-Key — quality-bar dimension 2
# ---------------------------------------------------------------------------


class TestIdempotencyKey:
    def test_default_key_derived_from_receipt(self):
        adapter = _adapter([(200, "ok", {})])
        adapter.deliver_sync(
            recipient="#x",
            receipt_id="rcpt-42",
            classification=Classification.INTERNAL,
            priority="normal",
            summary="x",
        )
        headers = adapter._poster.calls[0]["headers"]  # type: ignore[attr-defined]
        assert "Idempotency-Key" in headers
        # Default key should embed the receipt id so retries dedupe.
        assert "rcpt-42" in headers["Idempotency-Key"]

    def test_caller_can_override_idempotency_key(self):
        adapter = _adapter([(200, "ok", {})])
        adapter.deliver_sync(
            recipient="#x",
            receipt_id="rcpt-42",
            classification=Classification.INTERNAL,
            priority="normal",
            summary="x",
            idempotency_key="caller-supplied-key",
        )
        headers = adapter._poster.calls[0]["headers"]  # type: ignore[attr-defined]
        assert headers["Idempotency-Key"] == "caller-supplied-key"


# ---------------------------------------------------------------------------
# Happy path + non-retryable failures
# ---------------------------------------------------------------------------


class TestDeliveryOutcomes:
    def test_happy_path_first_attempt(self):
        sleeper = _RecordingSleeper()
        adapter = _adapter([(200, "ok", {})], sleeper=sleeper)
        result = adapter.deliver_sync(
            recipient="#general",
            receipt_id="r-1",
            classification=Classification.INTERNAL,
            priority="normal",
            summary="hi",
        )
        assert result.ok is True
        assert result.status_code == 200
        assert result.retry_attempts == 0
        assert result.reconnect_required is False
        # No retry sleeps on the happy path.
        assert sleeper.calls == []

    def test_non_retryable_400_returns_failure(self):
        sleeper = _RecordingSleeper()
        adapter = _adapter([(400, "bad payload", {})], sleeper=sleeper)
        result = adapter.deliver_sync(
            recipient="#x", receipt_id="r", classification=Classification.INTERNAL,
            priority="normal", summary="x",
        )
        assert result.ok is False
        assert result.status_code == 400
        # No retries for non-retryable 4xx.
        assert sleeper.calls == []


# ---------------------------------------------------------------------------
# ReconnectRequired — auth-class failure short-circuit
# ---------------------------------------------------------------------------


class TestReconnectRequired:
    @pytest.mark.parametrize("status", [401, 403])
    def test_auth_class_returns_reconnect_required(self, status):
        sleeper = _RecordingSleeper()
        adapter = _adapter([(status, "unauthorized", {})], sleeper=sleeper)
        result = adapter.deliver_sync(
            recipient="#x", receipt_id="r", classification=Classification.INTERNAL,
            priority="normal", summary="x",
        )
        assert result.ok is False
        assert result.reconnect_required is True
        assert result.status_code == status
        # Auth failures do NOT retry — the agent-bridge needs to see the
        # typed event so it routes to inbox + status surface.
        assert sleeper.calls == []


# ---------------------------------------------------------------------------
# Retry-After + exponential backoff
# ---------------------------------------------------------------------------


class TestRetryAfter:
    def test_429_with_retry_after_honored(self):
        sleeper = _RecordingSleeper()
        adapter = _adapter(
            [
                (429, "rate-limited", {"Retry-After": "2"}),
                (200, "ok", {}),
            ],
            sleeper=sleeper,
        )
        result = adapter.deliver_sync(
            recipient="#x", receipt_id="r", classification=Classification.INTERNAL,
            priority="normal", summary="x",
        )
        assert result.ok is True
        assert result.retry_attempts == 1
        # Sleeper was called with the Retry-After value, not exponential.
        assert sleeper.calls == [2.0]

    def test_503_without_retry_after_exponential(self):
        sleeper = _RecordingSleeper()
        adapter = _adapter(
            [
                (503, "unavailable", {}),
                (200, "ok", {}),
            ],
            sleeper=sleeper,
        )
        result = adapter.deliver_sync(
            recipient="#x", receipt_id="r", classification=Classification.INTERNAL,
            priority="normal", summary="x",
        )
        assert result.ok is True
        # First retry uses base backoff (1.0s).
        assert sleeper.calls == [1.0]

    def test_retry_after_capped_at_max(self):
        # 9999s requested → cap kicks in (max 30s).
        sleeper = _RecordingSleeper()
        adapter = _adapter(
            [
                (429, "x", {"Retry-After": "9999"}),
                (200, "ok", {}),
            ],
            sleeper=sleeper,
        )
        adapter.deliver_sync(
            recipient="#x", receipt_id="r", classification=Classification.INTERNAL,
            priority="normal", summary="x",
        )
        assert sleeper.calls == [30.0]

    def test_max_attempts_then_give_up(self):
        sleeper = _RecordingSleeper()
        adapter = _adapter(
            [
                (503, "down", {}),
                (503, "down", {}),
                (503, "down", {}),
            ],
            sleeper=sleeper,
        )
        result = adapter.deliver_sync(
            recipient="#x", receipt_id="r", classification=Classification.INTERNAL,
            priority="normal", summary="x",
        )
        assert result.ok is False
        # 3 attempts = 2 retries with sleeps; 3rd attempt returns the
        # error rather than retrying again.
        assert len(sleeper.calls) == 2
        assert result.retry_attempts == 2
        assert result.status_code == 503

    def test_network_exception_is_retried(self):
        sleeper = _RecordingSleeper()
        adapter = _adapter(
            [
                ConnectionError("dns fail"),
                (200, "ok", {}),
            ],
            sleeper=sleeper,
        )
        result = adapter.deliver_sync(
            recipient="#x", receipt_id="r", classification=Classification.INTERNAL,
            priority="normal", summary="x",
        )
        assert result.ok is True
        assert sleeper.calls == [1.0]


# ---------------------------------------------------------------------------
# Secret-redaction — the bar's invariant
# ---------------------------------------------------------------------------


class TestSecretRedaction:
    def test_sig_token_redacted_from_error(self):
        adapter = _adapter([(500, f"failed for {WEBHOOK}", {})])
        result = adapter.deliver_sync(
            recipient="#x", receipt_id="r", classification=Classification.INTERNAL,
            priority="normal", summary="x",
        )
        assert "SECRETSIGTOKEN" not in (result.error or "")

    def test_invoke_id_redacted_from_error(self):
        adapter = _adapter([(500, f"failed for {WEBHOOK}", {})])
        result = adapter.deliver_sync(
            recipient="#x", receipt_id="r", classification=Classification.INTERNAL,
            priority="normal", summary="x",
        )
        assert "long-invoke-id-12345" not in (result.error or "")
