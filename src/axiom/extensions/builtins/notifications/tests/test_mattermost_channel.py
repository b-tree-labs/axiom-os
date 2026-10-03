# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tests for ``MattermostChannelAdapter`` — same contract as Slack;
self-hosted Mattermost (any host) + Slack-compatible webhook surface."""

from __future__ import annotations

import pytest

from axiom.extensions.builtins.notifications.channels.base import Direction
from axiom.extensions.builtins.notifications.channels.mattermost import (
    MattermostChannelAdapter,
    MattermostChannelAdapterProvider,
)
from axiom.governance import Classification


class _FakePoster:
    def __init__(self, *, status_code=200, body="ok", raise_exc=None):
        self.status_code = status_code
        self.body = body
        self.raise_exc = raise_exc
        self.calls = []

    def post(self, url, json, timeout):
        self.calls.append({"url": url, "json": json, "timeout": timeout})
        if self.raise_exc is not None:
            raise self.raise_exc
        return _FakeResp(self.status_code, self.body)


class _FakeResp:
    def __init__(self, status_code, text):
        self.status_code = status_code
        self.text = text


WEBHOOK = "https://mm.example.com/hooks/abc123token456"


def _adapter(poster=None):
    return MattermostChannelAdapter(
        webhook_url=WEBHOOK, poster=poster or _FakePoster()
    )


class TestMattermostProvider:
    def test_name(self):
        assert MattermostChannelAdapterProvider().name == "mattermost"

    def test_capabilities(self):
        caps = MattermostChannelAdapterProvider().capabilities()
        assert caps.direction == Direction.OUTBOUND
        assert caps.classification_ceiling == Classification.INTERNAL
        assert caps.supports_threading is True
        assert caps.supports_acknowledge is False

    def test_build_requires_webhook(self):
        with pytest.raises(ValueError, match="webhook_url"):
            MattermostChannelAdapterProvider().build({})

    def test_build_self_hosted_host_ok(self):
        # Critical: Mattermost adapter must accept any host (self-hosted).
        adapter = MattermostChannelAdapterProvider().build(
            {"webhook_url": "https://chat.acme.internal/hooks/xyz"}
        )
        assert adapter.name == "mattermost"


class TestMattermostDeliverSync:
    def test_happy_path_posts_to_webhook(self):
        poster = _FakePoster()
        result = _adapter(poster).deliver_sync(
            recipient="#general",
            receipt_id="rcpt-1",
            classification=Classification.INTERNAL,
            priority="normal",
            summary="DP1 RAG ingest complete",
        )
        assert result.ok is True
        assert poster.calls[0]["url"] == WEBHOOK
        assert "DP1 RAG ingest complete" in poster.calls[0]["json"]["text"]

    def test_channel_override_strips_hash(self):
        # Mattermost expects channel names without the leading "#".
        poster = _FakePoster()
        _adapter(poster).deliver_sync(
            recipient="#alerts",
            receipt_id="rcpt-2",
            classification=Classification.INTERNAL,
            priority="normal",
            summary="x",
        )
        assert poster.calls[0]["json"]["channel"] == "alerts"

    def test_dm_recipient_passes_through_without_channel_override(self):
        # If recipient doesn't start with "#", don't set channel —
        # mattermost will use the webhook's default.
        poster = _FakePoster()
        _adapter(poster).deliver_sync(
            recipient="@bbooth",
            receipt_id="rcpt-3",
            classification=Classification.INTERNAL,
            priority="normal",
            summary="x",
        )
        assert "channel" not in poster.calls[0]["json"]

    def test_urgent_priority_marker_in_payload(self):
        poster = _FakePoster()
        _adapter(poster).deliver_sync(
            recipient="#general",
            receipt_id="rcpt-4",
            classification=Classification.INTERNAL,
            priority="urgent",
            summary="trunk red",
        )
        text = poster.calls[0]["json"]["text"]
        assert "trunk red" in text
        assert any(m in text.lower() for m in ("urgent", "🚨"))

    def test_4xx_returns_failure(self):
        result = _adapter(_FakePoster(status_code=401, body="bad token")).deliver_sync(
            recipient="#x", receipt_id="r", classification=Classification.INTERNAL,
            priority="normal", summary="x",
        )
        assert result.ok is False
        assert "401" in result.error

    def test_5xx_returns_failure(self):
        result = _adapter(_FakePoster(status_code=503, body="unavailable")).deliver_sync(
            recipient="#x", receipt_id="r", classification=Classification.INTERNAL,
            priority="normal", summary="x",
        )
        assert result.ok is False
        assert "503" in result.error

    def test_network_exception_returns_failure(self):
        result = _adapter(_FakePoster(raise_exc=OSError("no route"))).deliver_sync(
            recipient="#x", receipt_id="r", classification=Classification.INTERNAL,
            priority="normal", summary="x",
        )
        assert result.ok is False
        assert "no route" in result.error or "OSError" in result.error

    def test_webhook_token_redacted_in_error(self):
        # The `/hooks/<token>` token must never round-trip through errors.
        result = _adapter(
            _FakePoster(status_code=500, body=f"see {WEBHOOK}")
        ).deliver_sync(
            recipient="#x", receipt_id="r", classification=Classification.INTERNAL,
            priority="normal", summary="x",
        )
        assert "abc123token456" not in (result.error or "")
