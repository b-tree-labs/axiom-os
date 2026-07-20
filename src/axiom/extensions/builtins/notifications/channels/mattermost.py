# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``mattermost`` channel adapter — HERALD-2a outbound via incoming webhook.

Sibling to ``SlackChannelAdapter``. Mattermost's incoming-webhook surface
is intentionally Slack-compatible at the wire level (``text`` field,
attachments / props), so the implementation is near-isomorphic. Key
differences:

- Webhook host varies per deployment (self-hosted) — secret-redaction
  must accept any host, not just ``hooks.slack.com``.
- ``channel`` override is per-call rather than fixed by the webhook
  (Mattermost permits redirecting to any joined channel).
- Mattermost markdown is more permissive (newlines, code blocks) and
  is sent in the ``text`` field directly rather than via blocks.

Per spec §9 the ceiling is ``INTERNAL``. Bidirectional (slash commands,
outgoing webhooks, websocket events) lands in HERALD-2b.
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


@dataclass
class MattermostDispatchResult:
    ok: bool
    error: str | None = None
    status_code: int | None = None


class _HttpPoster(Protocol):
    def post(self, url: str, json: dict, timeout: float): ...


def _default_poster() -> _HttpPoster:
    import httpx

    return httpx.Client(follow_redirects=False)


# Match any Mattermost-style incoming-webhook path. We do not know the
# user's host, so the redaction operates on the path segment that holds
# the secret (``/hooks/<token>``) regardless of host.
_MM_WEBHOOK_TOKEN_RE = re.compile(
    r"(/hooks/)[A-Za-z0-9]+"
)


def _strip_webhook(text: str) -> str:
    """Redact any ``/hooks/<token>`` path segments from error text."""
    return _MM_WEBHOOK_TOKEN_RE.sub(r"\1***", text)


_URGENCY_PREFIX = {
    "urgent": "🚨 URGENT — ",
    "high": "⚠️ HIGH — ",
    "normal": "",
    "low": "",
}


class MattermostChannelAdapter:
    """Outbound HERALD adapter for Mattermost incoming webhooks."""

    name = "mattermost"

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
    ) -> MattermostDispatchResult:
        prefix = _URGENCY_PREFIX.get(priority, "")
        # Mattermost accepts the Slack-compatible payload shape; we use
        # `text` (markdown) for the body + a context props block for the
        # operator-facing metadata.
        payload = {
            # Channel override — Mattermost permits redirecting to any
            # channel the webhook has access to. If recipient looks like
            # a channel name (`#xxx` → `xxx`), forward it.
            "channel": recipient.lstrip("#") if recipient.startswith("#") else None,
            "text": (
                f"**{prefix}{summary}**\n"
                f"_to_ `{recipient}` · _priority_ `{priority}` · "
                f"_classification_ `{classification.value}` · "
                f"_receipt_ `{receipt_id}`"
            ),
        }
        # Drop None values — Mattermost rejects them on some versions.
        payload = {k: v for k, v in payload.items() if v is not None}
        if sender is not None:
            from axiom.extensions.builtins.notifications.sender import render_for_channel
            _rs = render_for_channel(sender, "mattermost")
            payload["username"] = _rs.username
            if _rs.icon_url:
                payload["icon_url"] = _rs.icon_url

        try:
            resp = self._poster.post(
                self._webhook_url, json=payload, timeout=self._timeout
            )
        except Exception as exc:  # noqa: BLE001
            return MattermostDispatchResult(
                ok=False,
                error=_strip_webhook(f"{type(exc).__name__}: {exc}"),
            )

        if 200 <= resp.status_code < 300:
            return MattermostDispatchResult(
                ok=True, status_code=resp.status_code
            )

        body = getattr(resp, "text", "") or ""
        return MattermostDispatchResult(
            ok=False,
            status_code=resp.status_code,
            error=_strip_webhook(f"HTTP {resp.status_code}: {body[:200]}"),
        )


class MattermostChannelAdapterProvider:
    """Factory; secret is the ``/hooks/<token>`` path segment."""

    name = "mattermost"

    def capabilities(self) -> ChannelCapabilities:
        return ChannelCapabilities(
            name="mattermost",
            direction=Direction.OUTBOUND,
            priority_levels=("low", "normal", "high", "urgent"),
            classification_ceiling=Classification.INTERNAL,
            supports_threading=True,
            supports_acknowledge=False,
            delivery_sla_p95_ms=2000,
            connector_ref="mattermost-webhook",
        )

    def build(
        self, config: dict[str, Any] | None = None
    ) -> MattermostChannelAdapter:
        cfg = config or {}
        webhook_url = cfg.get("webhook_url")
        if not webhook_url:
            raise ValueError(
                "mattermost channel requires `webhook_url` in config; "
                "resolve via `axi secrets resolve mattermost-webhook-<workspace>`"
            )
        return MattermostChannelAdapter(
            webhook_url=webhook_url,
            poster=cfg.get("poster"),
            timeout=cfg.get("timeout", 10.0),
        )


__all__ = [
    "MattermostChannelAdapter",
    "MattermostChannelAdapterProvider",
    "MattermostDispatchResult",
]
