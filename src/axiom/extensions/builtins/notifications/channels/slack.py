# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``slack`` channel adapter — HERALD-2 outbound via incoming webhook.

First real channel adapter behind ``inbox``. v0 covers the outbound
half: incoming-webhook POST with a text + minimal block payload. Bot
Token + Events API (DM/mention/threading reply ingest) is HERALD-2b.

The provider/adapter shape mirrors ``InboxChannelAdapterProvider`` so
the registry (``ChannelAdapterRegistry``) treats slack as just one more
admitted channel under the classification-ceiling routing rule from
spec §4.

Per spec §9, the ceiling is ``INTERNAL`` — slack is not an admitted
channel for ``CONTROLLED`` envelopes. KEEP-bound capability tokens for
the webhook secret are HERALD-2b once the secrets connector handoff is
locked; today the webhook URL is passed at ``build()`` time from the
caller's secret resolution (typically ``axi secrets resolve``).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from axiom.extensions.builtins.notifications.sender import SenderIdentity

import re
from dataclasses import dataclass
from typing import Any, Protocol

from axiom.extensions.builtins.notifications.channels.base import (
    ChannelCapabilities,
    Direction,
)
from axiom.governance import Classification

# ---------------------------------------------------------------------------
# Result
# ---------------------------------------------------------------------------


@dataclass
class SlackDispatchResult:
    """Mirrors ``InboxDispatchResult`` so the send() dispatcher is uniform."""

    ok: bool
    error: str | None = None
    status_code: int | None = None


# ---------------------------------------------------------------------------
# HTTP poster Protocol — keeps the adapter testable + offline
# ---------------------------------------------------------------------------


class _HttpPoster(Protocol):
    def post(self, url: str, json: dict, timeout: float): ...


def _default_poster() -> _HttpPoster:
    """Lazy httpx import so the channel module loads even on minimal envs."""
    import httpx

    return httpx.Client(follow_redirects=False)


# ---------------------------------------------------------------------------
# Webhook-URL secret-stripping for error messages
# ---------------------------------------------------------------------------


_SLACK_WEBHOOK_PATH_RE = re.compile(
    r"https?://hooks\.slack\.com/services/[A-Za-z0-9/_-]+"
)


def _strip_webhook(text: str) -> str:
    """Replace any Slack webhook URL substrings with a placeholder.

    The receiving adapter sees a body Slack returns on error which can
    occasionally echo the request URL. The HERALD contract is that
    secrets never round-trip through receipts or logs.
    """
    return _SLACK_WEBHOOK_PATH_RE.sub("https://hooks.slack.com/services/***", text)


# ---------------------------------------------------------------------------
# Adapter
# ---------------------------------------------------------------------------


_URGENCY_PREFIX = {
    "urgent": "🚨 URGENT — ",
    "high": "⚠️ HIGH — ",
    "normal": "",
    "low": "",
}


class SlackChannelAdapter:
    """Outbound HERALD adapter for Slack incoming webhooks."""

    name = "slack"

    def __init__(
        self,
        *,
        webhook_url: str,
        poster: _HttpPoster | None = None,
        timeout: float = 10.0,
    ) -> None:
        self._webhook_url = webhook_url
        self._poster = poster or _default_poster()
        self._timeout = timeout

    def deliver_sync(
        self,
        *,
        recipient: str,
        receipt_id: str,
        classification: Classification,
        priority: str,
        summary: str,
        sender: SenderIdentity | None = None,
    ) -> SlackDispatchResult:
        """POST to the configured webhook; return a structured result.

        Failures (HTTP non-2xx, network error) become structured failures
        instead of raised exceptions — matches the inbox adapter contract
        so the send() dispatcher can iterate channels uniformly.
        """
        prefix = _URGENCY_PREFIX.get(priority, "")
        payload = {
            # `text` is the fallback summary line Slack uses in
            # notifications + accessibility surfaces.
            "text": f"{prefix}{summary}",
            # Light context: the recipient hint (a channel like
            # "#general") rides in attachments rather than overriding
            # the webhook's destination, which the webhook URL fixes.
            "blocks": [
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": f"*{prefix}{summary}*",
                    },
                },
                {
                    "type": "context",
                    "elements": [
                        {
                            "type": "mrkdwn",
                            "text": (
                                f"to `{recipient}` · priority `{priority}` · "
                                f"classification `{classification.value}` · "
                                f"receipt `{receipt_id}`"
                            ),
                        }
                    ],
                },
            ],
        }

        if sender is not None:
            from axiom.extensions.builtins.notifications.sender import render_for_channel
            _rs = render_for_channel(sender, "slack")
            payload["username"] = _rs.username
            if _rs.icon_url:
                payload["icon_url"] = _rs.icon_url

        try:
            resp = self._poster.post(
                self._webhook_url, json=payload, timeout=self._timeout
            )
        except Exception as exc:  # noqa: BLE001 — adapter boundary
            return SlackDispatchResult(
                ok=False,
                error=_strip_webhook(f"{type(exc).__name__}: {exc}"),
            )

        if 200 <= resp.status_code < 300:
            return SlackDispatchResult(ok=True, status_code=resp.status_code)

        body = getattr(resp, "text", "") or ""
        return SlackDispatchResult(
            ok=False,
            status_code=resp.status_code,
            error=_strip_webhook(f"HTTP {resp.status_code}: {body[:200]}"),
        )


# ---------------------------------------------------------------------------
# Provider
# ---------------------------------------------------------------------------


class SlackChannelAdapterProvider:
    """Factory; ``build()`` receives the webhook URL from caller config.

    The webhook URL is a long-lived secret. In production the caller
    resolves it via the secrets extension's ``SecretBackendProvider``
    (OS keychain / HashiCorp / etc. per SEC-1). The provider does not
    cache the secret; ``build()`` returns a fresh adapter per call.
    """

    name = "slack"

    def capabilities(self) -> ChannelCapabilities:
        return ChannelCapabilities(
            name="slack",
            # v0 ships outbound only; bidirectional flips to true when
            # HERALD-2b adds the Events API ingest.
            direction=Direction.OUTBOUND,
            priority_levels=("low", "normal", "high", "urgent"),
            # Per spec §9: slack ceiling is INTERNAL.
            classification_ceiling=Classification.INTERNAL,
            # Threading via ``thread_ts`` is supported by the webhook
            # transport; v0 doesn't emit one yet but the capability is
            # declared so the dispatcher can route threadable intents.
            supports_threading=True,
            # Reaction/views-based ack lands in HERALD-2b alongside
            # Events API.
            supports_acknowledge=False,
            delivery_sla_p95_ms=2000,
            connector_ref="slack-webhook",
        )

    def build(self, config: dict[str, Any] | None = None) -> SlackChannelAdapter:
        cfg = config or {}
        webhook_url = cfg.get("webhook_url")
        if not webhook_url:
            raise ValueError(
                "slack channel requires `webhook_url` in config; "
                "resolve via `axi secrets resolve slack-webhook-<workspace>`"
            )
        poster = cfg.get("poster")  # tests inject a stub
        timeout = cfg.get("timeout", 10.0)
        return SlackChannelAdapter(
            webhook_url=webhook_url, poster=poster, timeout=timeout
        )


__all__ = [
    "SlackChannelAdapter",
    "SlackChannelAdapterProvider",
    "SlackDispatchResult",
]
