# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``teams`` channel adapter — Microsoft Teams Workflows webhook.

Microsoft retired the legacy ``outlook.office.com/webhook`` Office-365
Connector surface on 2026-05-22. The replacement is a Workflows-backed
webhook URL (``<tenant>.logic.azure.com/workflows/.../triggers/manual``)
which accepts the same Adaptive Card payload that "Post to a Teams
channel when a webhook request is received" templates produce.

This adapter targets the new surface. It also embodies the first
**connector-quality bar** uplift from the 2026-06-01 study:

- ``Retry-After`` parsing with capped exponential backoff (max 3 attempts)
- ``ReconnectRequired`` typed error on 401 / 403 so the agent-bridge
  can route to the inbox + status surface rather than retry silently
- Idempotency key passthrough via the ``Idempotency-Key`` header (Logic
  Apps respects it on the workflow trigger)
- Secret-redaction of the workflow URL path components (``sig`` query
  param + invoke-id path segment) on every error path

Per spec §9 the ceiling stays ``INTERNAL`` (matches the other chat
channels). Inbound reply ingest via Bot Framework lands in HERALD-2b.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from axiom.extensions.builtins.notifications.sender import SenderIdentity

import re
import time
import uuid
from dataclasses import dataclass
from typing import Any, Protocol

from axiom.extensions.builtins.notifications.channels.base import (
    ChannelCapabilities,
    Direction,
)
from axiom.governance import Classification

# ---------------------------------------------------------------------------
# Results — typed errors so the dispatcher can branch
# ---------------------------------------------------------------------------


@dataclass
class TeamsDispatchResult:
    """Mirrors ``SlackDispatchResult`` shape + adds retry telemetry."""

    ok: bool
    error: str | None = None
    status_code: int | None = None
    retry_attempts: int = 0
    """Number of retry passes the adapter performed before returning."""
    reconnect_required: bool = False
    """True when the vendor returned 401/403 — operator must reconnect.
    The agent-bridge routes these to inbox + the connector-status surface
    so the operator sees a typed event rather than silent retries."""


# ---------------------------------------------------------------------------
# HTTP Protocol + secret-redaction
# ---------------------------------------------------------------------------


class _HttpPoster(Protocol):
    def post(self, url: str, json: dict, headers: dict, timeout: float): ...


def _default_poster() -> _HttpPoster:
    import httpx

    return httpx.Client(follow_redirects=False)


# Redact the Logic Apps ``sig=`` query-string token + the invoke-id path
# segment (``/triggers/manual/paths/invoke/<id>``) on errors.
_SIG_RE = re.compile(r"(sig=)[^&\s]+")
_INVOKE_RE = re.compile(r"(invoke/)[^/?\s]+")


def _strip_secret(text: str) -> str:
    text = _SIG_RE.sub(r"\1***", text)
    text = _INVOKE_RE.sub(r"\1***", text)
    return text


# ---------------------------------------------------------------------------
# Retry / backoff
# ---------------------------------------------------------------------------


_MAX_ATTEMPTS = 3
_BASE_BACKOFF_S = 1.0
_MAX_BACKOFF_S = 30.0
_RECONNECT_STATUSES = {401, 403}


def _parse_retry_after(value: str | None) -> float | None:
    """Parse ``Retry-After`` per RFC-7231 §7.1.3 — seconds (int) form only.

    HTTP-date form is rare in practice for retry-after; ignoring it is
    acceptable degradation. Returns capped seconds or None if unparseable.
    """
    if not value:
        return None
    try:
        secs = float(value.strip())
    except ValueError:
        return None
    return max(0.0, min(secs, _MAX_BACKOFF_S))


def _backoff_for(attempt: int, retry_after: float | None) -> float:
    """Exponential backoff with retry-after override and cap."""
    if retry_after is not None:
        return retry_after
    return min(_BASE_BACKOFF_S * (2 ** (attempt - 1)), _MAX_BACKOFF_S)


# ---------------------------------------------------------------------------
# Adaptive Card payload shape
# ---------------------------------------------------------------------------


_URGENCY_COLOR = {
    "urgent": "attention",
    "high": "warning",
    "normal": "default",
    "low": "accent",
}


def _build_adaptive_card(
    *,
    summary: str,
    recipient: str,
    receipt_id: str,
    classification: Classification,
    priority: str,
) -> dict[str, Any]:
    """Build an Adaptive Card payload for the Workflows trigger.

    Schema is the Workflows-trigger expected envelope: outer ``type``
    + ``attachments[0].contentType`` + ``contentUrl`` + ``content``
    where content is the Adaptive Card body.
    """
    color = _URGENCY_COLOR.get(priority, "default")
    return {
        "type": "message",
        "attachments": [
            {
                "contentType": "application/vnd.microsoft.card.adaptive",
                "contentUrl": None,
                "content": {
                    "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
                    "type": "AdaptiveCard",
                    "version": "1.5",
                    "body": [
                        {
                            "type": "TextBlock",
                            "size": "medium",
                            "weight": "bolder",
                            "color": color,
                            "text": summary,
                            "wrap": True,
                        },
                        {
                            "type": "FactSet",
                            "facts": [
                                {"title": "to", "value": recipient},
                                {"title": "priority", "value": priority},
                                {
                                    "title": "classification",
                                    "value": classification.value,
                                },
                                {"title": "receipt", "value": receipt_id},
                            ],
                        },
                    ],
                },
            }
        ],
    }


# ---------------------------------------------------------------------------
# Adapter
# ---------------------------------------------------------------------------


class TeamsChannelAdapter:
    """Outbound HERALD adapter for Microsoft Teams Workflows webhooks.

    The Workflows trigger URL is the secret. Per the quality bar:
    ``Retry-After`` is honored on 429 / 503, exponential backoff with
    a 30-second cap otherwise, max 3 attempts. 401 / 403 short-circuit
    with ``reconnect_required=True``.
    """

    name = "teams"

    def __init__(
        self,
        *,
        webhook_url: str,
        poster: _HttpPoster | None = None,
        timeout: float = 10.0,
        sleeper=time.sleep,
    ) -> None:
        self._webhook_url = webhook_url
        self._poster = poster or _default_poster()
        self._timeout = timeout
        self._sleeper = sleeper

    def deliver_sync(
        self,
        *,
        recipient: str,
        receipt_id: str,
        classification: Classification,
        priority: str,
        summary: str,
        idempotency_key: str | None = None,
        sender: SenderIdentity | None = None,
    ) -> TeamsDispatchResult:
        payload = _build_adaptive_card(
            summary=summary,
            recipient=recipient,
            receipt_id=receipt_id,
            classification=classification,
            priority=priority,
        )
        # Idempotency: default to a key derived from the receipt id so
        # retry duplication can't double-post even when the caller
        # doesn't supply a key. Logic Apps respects ``Idempotency-Key``.
        idem = idempotency_key or f"axiom-receipt-{receipt_id}-{uuid.uuid5(uuid.NAMESPACE_URL, receipt_id)}"
        headers = {
            "Content-Type": "application/json",
            "Idempotency-Key": idem,
        }

        last_error: str | None = None
        last_status: int | None = None
        for attempt in range(1, _MAX_ATTEMPTS + 1):
            try:
                resp = self._poster.post(
                    self._webhook_url,
                    json=payload,
                    headers=headers,
                    timeout=self._timeout,
                )
            except Exception as exc:  # noqa: BLE001
                last_error = _strip_secret(
                    f"{type(exc).__name__}: {exc}"
                )
                last_status = None
                # Network failures are retryable.
                if attempt < _MAX_ATTEMPTS:
                    self._sleeper(_backoff_for(attempt, None))
                    continue
                return TeamsDispatchResult(
                    ok=False,
                    error=last_error,
                    retry_attempts=attempt - 1,
                )

            status = resp.status_code
            last_status = status

            if 200 <= status < 300:
                return TeamsDispatchResult(
                    ok=True,
                    status_code=status,
                    retry_attempts=attempt - 1,
                )

            if status in _RECONNECT_STATUSES:
                # Auth-class failure — no retry; bubble up so the
                # operator + agent-bridge can route to inbox.
                body = getattr(resp, "text", "") or ""
                return TeamsDispatchResult(
                    ok=False,
                    status_code=status,
                    error=_strip_secret(
                        f"HTTP {status} (auth): {body[:200]}"
                    ),
                    retry_attempts=attempt - 1,
                    reconnect_required=True,
                )

            # Retryable: 429 + 5xx — honor Retry-After when present.
            if status == 429 or 500 <= status < 600:
                retry_after = _parse_retry_after(
                    _get_header(resp, "Retry-After")
                )
                body = getattr(resp, "text", "") or ""
                last_error = _strip_secret(
                    f"HTTP {status}: {body[:200]}"
                )
                if attempt < _MAX_ATTEMPTS:
                    self._sleeper(_backoff_for(attempt, retry_after))
                    continue
                return TeamsDispatchResult(
                    ok=False,
                    status_code=status,
                    error=last_error,
                    retry_attempts=attempt - 1,
                )

            # Non-retryable 4xx (400, 404, 422, …).
            body = getattr(resp, "text", "") or ""
            return TeamsDispatchResult(
                ok=False,
                status_code=status,
                error=_strip_secret(f"HTTP {status}: {body[:200]}"),
                retry_attempts=attempt - 1,
            )

        # Loop fell through — return last error.
        return TeamsDispatchResult(
            ok=False,
            status_code=last_status,
            error=last_error,
            retry_attempts=_MAX_ATTEMPTS - 1,
        )


def _get_header(resp: Any, name: str) -> str | None:
    """Tolerant header lookup over httpx/Response/dict-shape stubs."""
    headers = getattr(resp, "headers", None)
    if headers is None:
        return None
    try:
        # httpx + requests both implement __getitem__.
        return headers.get(name)
    except AttributeError:
        return dict(headers).get(name)


# ---------------------------------------------------------------------------
# Provider
# ---------------------------------------------------------------------------


class TeamsChannelAdapterProvider:
    """Factory; the Workflows trigger URL is passed at build time.

    The trigger URL is a long-lived secret (the ``sig`` query parameter
    is the bearer authority). Resolve via the secrets extension; do not
    bake into config files.
    """

    name = "teams"

    def capabilities(self) -> ChannelCapabilities:
        return ChannelCapabilities(
            name="teams",
            direction=Direction.OUTBOUND,
            priority_levels=("low", "normal", "high", "urgent"),
            classification_ceiling=Classification.INTERNAL,
            supports_threading=True,
            supports_acknowledge=False,
            delivery_sla_p95_ms=2500,
            connector_ref="teams-workflow-webhook",
        )

    def build(self, config: dict[str, Any] | None = None) -> TeamsChannelAdapter:
        cfg = config or {}
        webhook_url = cfg.get("webhook_url")
        if not webhook_url:
            raise ValueError(
                "teams channel requires `webhook_url` in config — the "
                "Workflows-trigger URL (post-2026-05-22 surface); "
                "resolve via `axi secrets resolve teams-workflow-<workspace>`"
            )
        return TeamsChannelAdapter(
            webhook_url=webhook_url,
            poster=cfg.get("poster"),
            timeout=cfg.get("timeout", 10.0),
            sleeper=cfg.get("sleeper", time.sleep),
        )


__all__ = [
    "TeamsChannelAdapter",
    "TeamsChannelAdapterProvider",
    "TeamsDispatchResult",
]
