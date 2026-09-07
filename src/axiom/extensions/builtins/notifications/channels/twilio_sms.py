# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``twilio-sms`` channel adapter — Twilio Programmable Messaging API.

Tier-A item #1 from the Radman connector blocker doc. The safety-
escalation tier the four chat channels can't reach (Slack / Teams /
Mattermost notifications on a phone are best-effort; an SMS lands on the
lock screen regardless of which apps the operator has open).

Built to the connector-quality bar from the 2026-06-01 study (§7):

- ``Retry-After`` parsing + capped exponential backoff (max 3 attempts)
- ``ReconnectRequired`` typed error on 401 / 403 (auth-token revoked)
- Secret redaction of ``AuthToken`` and ``AccountSid`` on every error path
- Twilio error-code surfacing (the 21xxx / 20xxx ``code`` field) on
  failure so operators can triage without re-fetching from Twilio logs

Twilio's Messages API doesn't accept an ``Idempotency-Key`` header
(unlike Stripe / Resend); receipt-id-driven dedup is enforced at the
``SendContext`` layer (fabric §6.1), not the adapter. That's the bar
deviation captured in the connector-quality study §8.

Per spec §9 ceiling is ``INTERNAL`` — matches the chat tier.
Bidirectional ingest (Twilio inbound SMS webhook → bus event) lands
in HERALD-2b.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from axiom.extensions.builtins.notifications.sender import SenderIdentity

import re
import time
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
class TwilioSmsDispatchResult:
    ok: bool
    error: str | None = None
    status_code: int | None = None
    message_sid: str | None = None
    """Twilio's ``SMxxx`` message identifier; populated on success."""
    twilio_code: int | None = None
    """Twilio's domain error code (e.g. 21211, 21610) on failure."""
    retry_attempts: int = 0
    reconnect_required: bool = False


# ---------------------------------------------------------------------------
# HTTP Protocol
# ---------------------------------------------------------------------------


class _HttpPoster(Protocol):
    def post(
        self,
        url: str,
        data: dict,
        auth: tuple[str, str],
        headers: dict,
        timeout: float,
    ): ...


def _default_poster() -> _HttpPoster:
    import httpx

    return httpx.Client(follow_redirects=False)


# ---------------------------------------------------------------------------
# Secret-redaction — AuthToken + AccountSid
# ---------------------------------------------------------------------------


# Twilio AccountSid: starts with ``AC`` + 32 hex chars.
_ACCOUNT_SID_RE = re.compile(r"AC[0-9a-fA-F]{32}")


def _build_secret_stripper(account_sid: str, auth_token: str):
    """Return a function that strips both secrets from an error string.

    AuthToken redaction is exact-match (it's a unique secret per account
    and any leak through error text is a credential disclosure). AccountSid
    is shape-matched so a body that references the sid by pattern (e.g.
    appears in a returned URL) also gets redacted.
    """

    def _strip(text: str) -> str:
        if not text:
            return text
        text = text.replace(auth_token, "***")
        # Strip both the exact sid + any other AC-shaped sid that appears.
        text = text.replace(account_sid, "AC***")
        text = _ACCOUNT_SID_RE.sub("AC***", text)
        return text

    return _strip


# ---------------------------------------------------------------------------
# Backoff
# ---------------------------------------------------------------------------


_MAX_ATTEMPTS = 3
_BASE_BACKOFF_S = 1.0
_MAX_BACKOFF_S = 30.0
_RECONNECT_STATUSES = {401, 403}


def _parse_retry_after(value: str | None) -> float | None:
    if not value:
        return None
    try:
        secs = float(value.strip())
    except ValueError:
        return None
    return max(0.0, min(secs, _MAX_BACKOFF_S))


def _backoff_for(attempt: int, retry_after: float | None) -> float:
    if retry_after is not None:
        return retry_after
    return min(_BASE_BACKOFF_S * (2 ** (attempt - 1)), _MAX_BACKOFF_S)


def _get_header(resp: Any, name: str) -> str | None:
    headers = getattr(resp, "headers", None)
    if headers is None:
        return None
    try:
        return headers.get(name)
    except AttributeError:
        return dict(headers).get(name)


# ---------------------------------------------------------------------------
# Body shaping — SMS is short; urgency in the prefix
# ---------------------------------------------------------------------------


_URGENCY_PREFIX = {
    "urgent": "🚨 URGENT — ",
    "high": "⚠️ HIGH — ",
    "normal": "",
    "low": "",
}


def _body_for(summary: str, priority: str) -> str:
    prefix = _URGENCY_PREFIX.get(priority, "")
    # SMS practical cap is 1600 chars (10 segments); truncate well below
    # to keep operator experience clean on lock screens.
    body = f"{prefix}{summary}"
    if len(body) > 320:
        body = body[:317] + "…"
    return body


# ---------------------------------------------------------------------------
# Adapter
# ---------------------------------------------------------------------------


class TwilioSmsChannelAdapter:
    """Outbound HERALD adapter for Twilio Programmable Messaging."""

    name = "twilio-sms"

    def __init__(
        self,
        *,
        account_sid: str,
        auth_token: str,
        from_number: str,
        poster: _HttpPoster | None = None,
        timeout: float = 10.0,
        sleeper=time.sleep,
    ) -> None:
        self._account_sid = account_sid
        self._auth_token = auth_token
        self._from_number = from_number
        self._poster = poster or _default_poster()
        self._timeout = timeout
        self._sleeper = sleeper
        self._strip = _build_secret_stripper(account_sid, auth_token)
        self._endpoint = (
            f"https://api.twilio.com/2010-04-01/Accounts/{account_sid}"
            "/Messages.json"
        )

    def deliver_sync(
        self,
        *,
        recipient: str,
        receipt_id: str,
        classification: Classification,
        priority: str,
        summary: str,
        sender: SenderIdentity | None = None,
    ) -> TwilioSmsDispatchResult:
        body = _body_for(summary, priority)
        if sender is not None:
            from axiom.extensions.builtins.notifications.sender import render_for_channel
            _rs = render_for_channel(sender, "twilio_sms")
            if _rs.body_prefix:
                body = f"{_rs.body_prefix} {body}"
        data = {
            "To": recipient,
            "From": self._from_number,
            "Body": body,
        }
        auth = (self._account_sid, self._auth_token)
        headers = {"Accept": "application/json"}

        last_error: str | None = None
        last_status: int | None = None
        last_twilio_code: int | None = None
        for attempt in range(1, _MAX_ATTEMPTS + 1):
            try:
                resp = self._poster.post(
                    self._endpoint,
                    data=data,
                    auth=auth,
                    headers=headers,
                    timeout=self._timeout,
                )
            except Exception as exc:  # noqa: BLE001
                last_error = self._strip(
                    f"{type(exc).__name__}: {exc}"
                )
                if attempt < _MAX_ATTEMPTS:
                    self._sleeper(_backoff_for(attempt, None))
                    continue
                return TwilioSmsDispatchResult(
                    ok=False,
                    error=last_error,
                    retry_attempts=attempt - 1,
                )

            status = resp.status_code
            last_status = status

            if 200 <= status < 300:
                payload = _safe_json(resp)
                return TwilioSmsDispatchResult(
                    ok=True,
                    status_code=status,
                    message_sid=(
                        payload.get("sid") if isinstance(payload, dict) else None
                    ),
                    retry_attempts=attempt - 1,
                )

            payload = _safe_json(resp)
            twilio_code = (
                payload.get("code")
                if isinstance(payload, dict) and isinstance(payload.get("code"), int)
                else None
            )
            last_twilio_code = twilio_code or last_twilio_code
            body_text = (
                getattr(resp, "text", "") or ""
            )

            if status in _RECONNECT_STATUSES:
                return TwilioSmsDispatchResult(
                    ok=False,
                    status_code=status,
                    error=self._strip(
                        f"HTTP {status} (auth): {body_text[:200]}"
                    ),
                    twilio_code=twilio_code,
                    retry_attempts=attempt - 1,
                    reconnect_required=True,
                )

            if status == 429 or 500 <= status < 600:
                retry_after = _parse_retry_after(
                    _get_header(resp, "Retry-After")
                )
                last_error = self._strip(
                    f"HTTP {status}: {body_text[:200]}"
                )
                if attempt < _MAX_ATTEMPTS:
                    self._sleeper(_backoff_for(attempt, retry_after))
                    continue
                return TwilioSmsDispatchResult(
                    ok=False,
                    status_code=status,
                    error=last_error,
                    twilio_code=twilio_code,
                    retry_attempts=attempt - 1,
                )

            # Non-retryable 4xx — typical Twilio domain errors land here
            # (21211 bad To, 21610 unsubscribed, etc.).
            return TwilioSmsDispatchResult(
                ok=False,
                status_code=status,
                error=self._strip(
                    f"HTTP {status}: {body_text[:200]}"
                ),
                twilio_code=twilio_code,
                retry_attempts=attempt - 1,
            )

        return TwilioSmsDispatchResult(
            ok=False,
            status_code=last_status,
            error=last_error,
            twilio_code=last_twilio_code,
            retry_attempts=_MAX_ATTEMPTS - 1,
        )


def _safe_json(resp: Any) -> Any:
    try:
        return resp.json()
    except Exception:  # noqa: BLE001
        return {}


# ---------------------------------------------------------------------------
# Provider
# ---------------------------------------------------------------------------


class TwilioSmsChannelAdapterProvider:
    """Factory. Twilio account credentials are passed at ``build()`` time
    from the caller's secret resolution (typically the secrets extension).

    UT and B-Tree deployments configure separate Twilio accounts; the
    provider is account-agnostic.
    """

    name = "twilio-sms"

    def capabilities(self) -> ChannelCapabilities:
        return ChannelCapabilities(
            name="twilio-sms",
            direction=Direction.OUTBOUND,
            priority_levels=("low", "normal", "high", "urgent"),
            # Per spec §9 — chat-tier ceiling; SMS is similar.
            classification_ceiling=Classification.INTERNAL,
            # SMS conversations have no thread_ts equivalent; conversation
            # tracking via Twilio Conversations API is HERALD-2b.
            supports_threading=False,
            # SMS replies arrive via Twilio inbound webhook → HERALD-2b.
            supports_acknowledge=False,
            delivery_sla_p95_ms=3000,
            connector_ref="twilio-account",
        )

    def build(
        self, config: dict[str, Any] | None = None
    ) -> TwilioSmsChannelAdapter:
        cfg = config or {}
        account_sid = cfg.get("account_sid")
        auth_token = cfg.get("auth_token")
        from_number = cfg.get("from_number")
        if not account_sid:
            raise ValueError(
                "twilio-sms channel requires `account_sid` in config; "
                "resolve via `axi secrets resolve twilio-account-sid`"
            )
        if not auth_token:
            raise ValueError(
                "twilio-sms channel requires `auth_token` in config; "
                "resolve via `axi secrets resolve twilio-auth-token`"
            )
        if not from_number:
            raise ValueError(
                "twilio-sms channel requires `from_number` in config "
                "(an E.164 number on the Twilio account, e.g. +15125550100)"
            )
        return TwilioSmsChannelAdapter(
            account_sid=account_sid,
            auth_token=auth_token,
            from_number=from_number,
            poster=cfg.get("poster"),
            timeout=cfg.get("timeout", 10.0),
            sleeper=cfg.get("sleeper", time.sleep),
        )


__all__ = [
    "TwilioSmsChannelAdapter",
    "TwilioSmsChannelAdapterProvider",
    "TwilioSmsDispatchResult",
]
