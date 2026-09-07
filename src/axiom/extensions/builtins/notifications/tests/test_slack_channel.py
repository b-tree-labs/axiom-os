# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tests for ``SlackChannelAdapter`` — the first real HERALD outbound adapter.

TDD-first per CLAUDE.md core invariants. Tests pin the contract:

- Provider exposes the right ``ChannelCapabilities`` (priority levels,
  classification ceiling, threading + ack capabilities, SLA).
- ``build()`` accepts a webhook URL via config OR rejects with a clear
  error when no webhook is configured.
- ``deliver_sync()`` POSTs the payload + returns a structured result.
- HTTP errors (4xx / 5xx / network) become structured failures, never
  raised exceptions (mirrors the inbox adapter contract).
- Constant-time secret handling — the webhook URL never appears in the
  returned receipt or error string.
"""

from __future__ import annotations

import pytest

from axiom.extensions.builtins.notifications.channels.base import (
    Direction,
)
from axiom.extensions.builtins.notifications.channels.slack import (
    SlackChannelAdapter,
    SlackChannelAdapterProvider,
)
from axiom.governance import Classification

# ---------------------------------------------------------------------------
# Stub HTTP poster so tests stay fast + deterministic + offline
# ---------------------------------------------------------------------------


class _FakePoster:
    """Captures every call; returns a configured response."""

    def __init__(self, *, status_code: int = 200, body: str = "ok",
                 raise_exc: Exception | None = None) -> None:
        self.status_code = status_code
        self.body = body
        self.raise_exc = raise_exc
        self.calls: list[dict] = []

    def post(self, url: str, json: dict, timeout: float) -> _FakeResp:
        self.calls.append({"url": url, "json": json, "timeout": timeout})
        if self.raise_exc is not None:
            raise self.raise_exc
        return _FakeResp(status_code=self.status_code, text=self.body)


class _FakeResp:
    def __init__(self, *, status_code: int, text: str) -> None:
        self.status_code = status_code
        self.text = text


# ---------------------------------------------------------------------------
# Provider — capabilities + build
# ---------------------------------------------------------------------------


class TestSlackProvider:
    def test_provider_name(self):
        assert SlackChannelAdapterProvider().name == "slack"

    def test_capabilities_shape(self):
        caps = SlackChannelAdapterProvider().capabilities()
        # v0 outbound only; bidirectional comes with Events API in HERALD-2b.
        assert caps.direction == Direction.OUTBOUND
        assert caps.priority_levels == ("low", "normal", "high", "urgent")
        # Per spec §9 — slack ceiling is INTERNAL, not CONTROLLED.
        assert caps.classification_ceiling == Classification.INTERNAL
        # Slack threading via thread_ts; ack via reactions/views is HERALD-2b.
        assert caps.supports_threading is True
        assert caps.supports_acknowledge is False
        assert caps.delivery_sla_p95_ms > 0

    def test_build_requires_webhook_url(self):
        with pytest.raises(ValueError, match="webhook_url"):
            SlackChannelAdapterProvider().build({})

    def test_build_returns_adapter(self):
        adapter = SlackChannelAdapterProvider().build(
            {"webhook_url": "https://hooks.slack.com/services/T/B/X"}
        )
        assert isinstance(adapter, SlackChannelAdapter)
        assert adapter.name == "slack"


# ---------------------------------------------------------------------------
# Adapter — happy path + failure modes
# ---------------------------------------------------------------------------


WEBHOOK = "https://hooks.slack.com/services/T123/B456/abcDEF"


def _adapter(poster: _FakePoster | None = None) -> SlackChannelAdapter:
    return SlackChannelAdapter(
        webhook_url=WEBHOOK,
        poster=poster or _FakePoster(),
    )


class TestSlackDeliverSync:
    def test_happy_path_posts_to_webhook(self):
        poster = _FakePoster()
        adapter = _adapter(poster)
        result = adapter.deliver_sync(
            recipient="#general",
            receipt_id="rcpt-1",
            classification=Classification.INTERNAL,
            priority="normal",
            summary="DP1 RAG ingest complete",
        )
        assert result.ok is True
        assert len(poster.calls) == 1
        call = poster.calls[0]
        assert call["url"] == WEBHOOK
        # Payload includes summary text + a recipient hint.
        assert "DP1 RAG ingest complete" in call["json"]["text"]

    def test_high_priority_flag_in_payload(self):
        poster = _FakePoster()
        adapter = _adapter(poster)
        adapter.deliver_sync(
            recipient="#general",
            receipt_id="rcpt-2",
            classification=Classification.INTERNAL,
            priority="urgent",
            summary="trunk red",
        )
        # Urgent priority surfaces visually (emoji or block-style)
        # without leaking into low-priority noise.
        payload = poster.calls[0]["json"]
        text = payload.get("text", "")
        assert "trunk red" in text
        # Marker: urgent priority is reflected somehow in the payload.
        assert any(
            marker in str(payload).lower()
            for marker in ("urgent", "🚨", "high", "alert")
        )

    def test_4xx_returns_failure_not_raise(self):
        poster = _FakePoster(status_code=403, body="invalid_token")
        adapter = _adapter(poster)
        result = adapter.deliver_sync(
            recipient="#x",
            receipt_id="rcpt-3",
            classification=Classification.INTERNAL,
            priority="normal",
            summary="x",
        )
        assert result.ok is False
        assert result.error is not None
        assert "403" in result.error

    def test_5xx_returns_failure_not_raise(self):
        poster = _FakePoster(status_code=502, body="bad gateway")
        adapter = _adapter(poster)
        result = adapter.deliver_sync(
            recipient="#x",
            receipt_id="rcpt-4",
            classification=Classification.INTERNAL,
            priority="normal",
            summary="x",
        )
        assert result.ok is False
        assert "502" in result.error

    def test_network_exception_returns_failure_not_raise(self):
        poster = _FakePoster(raise_exc=ConnectionError("dns fail"))
        adapter = _adapter(poster)
        result = adapter.deliver_sync(
            recipient="#x",
            receipt_id="rcpt-5",
            classification=Classification.INTERNAL,
            priority="normal",
            summary="x",
        )
        assert result.ok is False
        assert "dns fail" in result.error or "ConnectionError" in result.error

    def test_webhook_url_never_appears_in_result_or_error(self):
        # Secret-redaction invariant — the webhook URL is a long-lived
        # secret. It must not be reflected back through any logged path.
        poster = _FakePoster(status_code=500, body=f"failed for {WEBHOOK}")
        adapter = _adapter(poster)
        result = adapter.deliver_sync(
            recipient="#x",
            receipt_id="rcpt-6",
            classification=Classification.INTERNAL,
            priority="normal",
            summary="x",
        )
        # The body from Slack might echo the URL; the adapter must strip
        # it before returning. Stronger contract than "best effort": the
        # error must not contain the secret path component.
        assert "abcDEF" not in (result.error or "")
        assert "B456" not in (result.error or "")
