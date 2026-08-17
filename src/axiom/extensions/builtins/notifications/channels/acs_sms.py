# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0

"""``acs-sms`` channel adapter — Azure Communication Services SMS REST API.

Sibling of ``twilio_sms.py`` / ``aws_sns_sms.py``: the lock-screen
safety-escalation tier delivered through Azure Communication Services.

Auth is either the connection-string HMAC (default) or an Entra
(Azure AD) bearer token (``endpoint`` + ``access_token``). No Azure SDK
dependency — the signing lives in the shared ``channels/_acs.py`` helper
and the send is a single HTTPS POST (injectable poster for offline tests).

Built to the connector-quality bar (2026-06-01 study §7):

- ``Retry-After`` parsing + capped exponential backoff (max 3 attempts)
- ``reconnect_required`` on 401 / 403 (key revoked / token expired)
- Secret redaction of the access key on every error path
- ACS per-recipient result surfacing (``messageId`` + HTTP status)

Per spec §9 the ceiling is ``INTERNAL`` — external channel; ``regulated``
/ ``controlled`` (EC-controlled / ITAR) envelopes are never admitted and
fall back to the inbox channel (see ``send.py``).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from axiom.extensions.builtins.notifications.sender import SenderIdentity

import json as _json
import re
import time
from dataclasses import dataclass
from typing import Any, Protocol

from axiom.extensions.builtins.notifications.channels._acs import (
    parse_connection_string,
    rfc1123_now,
    sign_request,
)
from axiom.extensions.builtins.notifications.channels.base import (
    ChannelCapabilities,
    Direction,
)
from axiom.governance import Classification

_ACS_SMS_API_VERSION = "2021-03-07"

# ---------------------------------------------------------------------------
# Result
# ---------------------------------------------------------------------------


@dataclass
class AcsSmsDispatchResult:
    ok: bool
    error: str | None = None
    status_code: int | None = None
    message_id: str | None = None
    reconnect_required: bool = False
    retry_attempts: int = 0


# ---------------------------------------------------------------------------
# HTTP poster Protocol
# ---------------------------------------------------------------------------


class _HttpPoster(Protocol):
    def post(
        self, url: str, content: bytes, headers: dict, timeout: float
    ): ...


def _default_poster() -> Any:
    import httpx

    return httpx.Client(follow_redirects=False)


# ---------------------------------------------------------------------------
# Backoff (mirrors twilio_sms.py)
# ---------------------------------------------------------------------------


_MAX_ATTEMPTS = 3
_BASE_BACKOFF_S = 1.0
_MAX_BACKOFF_S = 30.0
_RECONNECT_STATUSES = {401, 403}

_ACCESSKEY_RE = re.compile(r"accesskey=[A-Za-z0-9+/=]+", re.IGNORECASE)

_URGENCY_PREFIX = {
    "urgent": "🚨 URGENT — ",
    "high": "⚠️ HIGH — ",
    "normal": "",
    "low": "",
}


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


def _body_for(summary: str, priority: str) -> str:
    prefix = _URGENCY_PREFIX.get(priority, "")
    body = f"{prefix}{summary}"
    if len(body) > 320:
        body = body[:317] + "…"
    return body


def _safe_json(resp: Any) -> Any:
    try:
        return resp.json()
    except Exception:  # noqa: BLE001
        return {}


# ---------------------------------------------------------------------------
# Adapter
# ---------------------------------------------------------------------------


class AcsSmsChannelAdapter:
    """Outbound HERALD adapter for Azure Communication Services SMS."""

    name = "acs-sms"

    def __init__(
        self,
        *,
        endpoint: str,
        from_number: str,
        access_key: str | None = None,
        access_token: str | None = None,
        poster: _HttpPoster | None = None,
        timeout: float = 10.0,
        sleeper=time.sleep,
        clock: Any | None = None,
    ) -> None:
        if not access_key and not access_token:
            raise ValueError(
                "acs-sms channel requires an HMAC access key "
                "(from `connection_string`) or an Entra `access_token`"
            )
        self._endpoint = endpoint.rstrip("/")
        self._from_number = from_number
        self._access_key = access_key
        self._access_token = access_token
        self._poster = poster or _default_poster()
        self._timeout = timeout
        self._sleeper = sleeper
        self._clock = clock

    def _strip(self, text: str) -> str:
        if not text:
            return text
        if self._access_key:
            text = text.replace(self._access_key, "***")
        if self._access_token:
            text = text.replace(self._access_token, "***")
        return _ACCESSKEY_RE.sub("accesskey=***", text)

    def _headers_for(self, url: str, body_bytes: bytes) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self._access_token:
            headers["Authorization"] = f"Bearer {self._access_token}"
        else:
            headers.update(
                sign_request(
                    access_key_b64=self._access_key or "",
                    method="POST",
                    url=url,
                    body=body_bytes,
                    date_str=rfc1123_now(self._clock),
                )
            )
        return headers

    def deliver_sync(
        self,
        *,
        recipient: str,
        receipt_id: str,
        classification: Classification,
        priority: str,
        summary: str,
        sender: SenderIdentity | None = None,
    ) -> AcsSmsDispatchResult:
        body = _body_for(summary, priority)
        if sender is not None:
            from axiom.extensions.builtins.notifications.sender import (
                render_for_channel,
            )

            _rs = render_for_channel(sender, "twilio_sms")
            if _rs.body_prefix:
                body = f"{_rs.body_prefix} {body}"

        url = f"{self._endpoint}/sms?api-version={_ACS_SMS_API_VERSION}"
        payload = {
            "from": self._from_number,
            "smsRecipients": [{"to": recipient}],
            "message": body,
            "smsSendOptions": {"enableDeliveryReport": False},
        }
        # Sign per-attempt because the HMAC binds the x-ms-date timestamp.
        last_error: str | None = None
        last_status: int | None = None
        for attempt in range(1, _MAX_ATTEMPTS + 1):
            body_bytes = _json.dumps(payload).encode("utf-8")
            headers = self._headers_for(url, body_bytes)
            try:
                resp = self._poster.post(
                    url,
                    content=body_bytes,
                    headers=headers,
                    timeout=self._timeout,
                )
            except Exception as exc:  # noqa: BLE001 — network boundary
                last_error = self._strip(f"{type(exc).__name__}: {exc}")
                if attempt < _MAX_ATTEMPTS:
                    self._sleeper(_backoff_for(attempt, None))
                    continue
                return AcsSmsDispatchResult(
                    ok=False, error=last_error, retry_attempts=attempt - 1
                )

            status = resp.status_code
            last_status = status

            if 200 <= status < 300:
                data = _safe_json(resp)
                message_id = _extract_message_id(data)
                return AcsSmsDispatchResult(
                    ok=True,
                    status_code=status,
                    message_id=message_id,
                    retry_attempts=attempt - 1,
                )

            body_text = getattr(resp, "text", "") or ""

            if status in _RECONNECT_STATUSES:
                return AcsSmsDispatchResult(
                    ok=False,
                    status_code=status,
                    error=self._strip(f"HTTP {status} (auth): {body_text[:200]}"),
                    reconnect_required=True,
                    retry_attempts=attempt - 1,
                )

            if status == 429 or 500 <= status < 600:
                retry_after = _parse_retry_after(_get_header(resp, "Retry-After"))
                last_error = self._strip(f"HTTP {status}: {body_text[:200]}")
                if attempt < _MAX_ATTEMPTS:
                    self._sleeper(_backoff_for(attempt, retry_after))
                    continue
                return AcsSmsDispatchResult(
                    ok=False,
                    status_code=status,
                    error=last_error,
                    retry_attempts=attempt - 1,
                )

            # Non-retryable 4xx (bad number, unverified sender, etc.).
            return AcsSmsDispatchResult(
                ok=False,
                status_code=status,
                error=self._strip(f"HTTP {status}: {body_text[:200]}"),
                retry_attempts=attempt - 1,
            )

        return AcsSmsDispatchResult(
            ok=False,
            status_code=last_status,
            error=last_error,
            retry_attempts=_MAX_ATTEMPTS - 1,
        )


def _extract_message_id(data: Any) -> str | None:
    """Pull the first per-recipient messageId out of an ACS SMS response."""
    if isinstance(data, dict):
        value = data.get("value")
        if isinstance(value, list) and value and isinstance(value[0], dict):
            mid = value[0].get("messageId")
            if isinstance(mid, str):
                return mid
    return None


# ---------------------------------------------------------------------------
# Provider
# ---------------------------------------------------------------------------


class AcsSmsChannelAdapterProvider:
    """Factory. Connection string OR endpoint+Entra-token are passed at
    ``build()`` time from the caller's secret resolution."""

    name = "acs-sms"

    def capabilities(self) -> ChannelCapabilities:
        return ChannelCapabilities(
            name="acs-sms",
            direction=Direction.OUTBOUND,
            priority_levels=("low", "normal", "high", "urgent"),
            # Per spec §9 — external channel; INTERNAL ceiling (open+internal).
            classification_ceiling=Classification.INTERNAL,
            supports_threading=False,
            supports_acknowledge=False,
            delivery_sla_p95_ms=3000,
            connector_ref="acs-account",
        )

    def build(self, config: dict[str, Any] | None = None) -> AcsSmsChannelAdapter:
        cfg = config or {}
        from_number = cfg.get("from_number")
        if not from_number:
            raise ValueError(
                "acs-sms channel requires `from_number` in config "
                "(an ACS-provisioned number, e.g. +15125550100)"
            )
        connection_string = cfg.get("connection_string")
        endpoint = cfg.get("endpoint")
        access_key = None
        if connection_string:
            endpoint, access_key = parse_connection_string(connection_string)
        if not endpoint:
            raise ValueError(
                "acs-sms channel requires `connection_string` or `endpoint`"
            )
        return AcsSmsChannelAdapter(
            endpoint=endpoint,
            from_number=from_number,
            access_key=access_key,
            access_token=cfg.get("access_token"),
            poster=cfg.get("poster"),
            timeout=cfg.get("timeout", 10.0),
            sleeper=cfg.get("sleeper", time.sleep),
            clock=cfg.get("clock"),
        )


__all__ = [
    "AcsSmsChannelAdapter",
    "AcsSmsChannelAdapterProvider",
    "AcsSmsDispatchResult",
]
