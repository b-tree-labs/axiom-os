# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0

"""Slack inbound — signing-secret verifier + Events API decoder (ADR-067 PR-4).

Slack signs each request with an HMAC-SHA256 over ``v0:{timestamp}:{body}``
keyed by the app signing secret, in the ``X-Slack-Signature`` header
(``v0=`` prefix) alongside ``X-Slack-Request-Timestamp``. We verify that
and reject stale timestamps (replay guard). The decoder normalizes the
Events API envelope (``app_mention`` / ``message.im``) into an
``InboundEvent``; the ``url_verification`` handshake is handled in the
route (it must echo the challenge, not publish).
"""

from __future__ import annotations

import hashlib
import hmac
import time
from collections.abc import Callable, Mapping
from typing import Any

from axiom.extensions.builtins.notifications.gateway.decode import InboundEvent

_MAX_SKEW_SECONDS = 60 * 5  # Slack's recommended replay window


class SlackSigningVerifier:
    """Verify ``X-Slack-Signature`` per Slack's v0 signing scheme.

    ``secret`` is the app signing secret (resolved from the secrets
    backend at registration time). ``clock`` is injectable for tests.
    """

    def __init__(
        self,
        secret: str,
        *,
        max_skew_seconds: int = _MAX_SKEW_SECONDS,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._secret = secret.encode("utf-8")
        self._max_skew = max_skew_seconds
        self._clock = clock

    def verify(self, *, headers: Mapping[str, str], body: bytes) -> bool:
        ts = headers.get("x-slack-request-timestamp", "")
        provided = headers.get("x-slack-signature", "")
        if not ts or not provided:
            return False
        try:
            skew = abs(self._clock() - float(ts))
        except ValueError:
            return False
        if skew > self._max_skew:
            return False  # replay / stale
        base = b"v0:" + ts.encode("utf-8") + b":" + body
        digest = hmac.new(self._secret, base, hashlib.sha256).hexdigest()
        expected = f"v0={digest}"
        return hmac.compare_digest(expected, provided)


class SlackDecoder:
    """Normalize a Slack Events API envelope into an ``InboundEvent``."""

    def ignore(self, vendor: str, body: dict[str, Any]) -> bool:
        """Drop bot-authored events so an agent never replies to itself.

        Without this, the bot's own reply is re-delivered as a message
        event, re-classified, and replied to again — an infinite loop.
        Every Slack bot needs this guard.
        """
        inner = body.get("event") or {}
        if not isinstance(inner, dict):
            return False
        return bool(inner.get("bot_id")) or inner.get("subtype") == "bot_message"

    def decode(self, vendor: str, body: dict[str, Any]) -> InboundEvent:
        event_id = str(body.get("event_id") or "")
        inner = body.get("event") or {}
        if not isinstance(inner, dict):
            inner = {}
        text = str(inner.get("text") or "")
        sender_ref = str(inner.get("user") or "")
        # Reply in-thread to the message's own ts when it isn't already
        # part of a thread (reply-bind-back, PR-9).
        thread_ref = inner.get("thread_ts") or inner.get("ts")
        channel = inner.get("channel")
        return InboundEvent(
            vendor=vendor,
            event_id=event_id,
            text=text,
            sender_ref=sender_ref,
            thread_ref=str(thread_ref) if thread_ref else None,
            channel=str(channel) if channel else None,
            raw=body,
        )


def is_url_verification(body: Any) -> str | None:
    """Return the challenge string if ``body`` is Slack's URL handshake.

    Slack posts ``{"type": "url_verification", "challenge": "..."}`` when
    you set the Request URL; the endpoint must echo the challenge back and
    must NOT treat it as an event.
    """
    if (
        isinstance(body, dict)
        and body.get("type") == "url_verification"
        and isinstance(body.get("challenge"), str)
    ):
        return body["challenge"]
    return None


__all__ = ["SlackSigningVerifier", "SlackDecoder", "is_url_verification"]
