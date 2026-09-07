# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0

"""``fcm-push`` channel adapter — Firebase Cloud Messaging HTTP v1.

Mobile push tier: a notification lands on the operator's device via
Firebase Cloud Messaging. The ``recipient`` is an FCM registration token
(a device) or a ``/topics/<name>`` subscription.

Per the connector pattern (fabric §5.3) we do NOT run the service-account
JWT → access-token dance here: the caller supplies an OAuth2 **access
token** (resolved/refreshed from the secrets store by the connector). The
send is a single HTTPS POST, so the base install stays lean (injectable
poster for offline tests) — mirrors ``slack.py`` / ``email/gmail.py``.

Per spec §9 the ceiling is ``INTERNAL`` — external channel; ``regulated``
/ ``controlled`` (EC-controlled / ITAR) envelopes are never admitted and
fall back to the inbox channel (see ``send.py``).

Note: there is deliberately **no "GCP SMS" adapter** — GCP has no native
SMS service (SMS on GCP means a third-party integration). SMS tiers are
Twilio / AWS SNS / Azure ACS.
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
class FcmPushDispatchResult:
    ok: bool
    error: str | None = None
    status_code: int | None = None
    message_id: str | None = None
    """FCM message name (``projects/<id>/messages/<mid>``) on success."""
    reconnect_required: bool = False


class _HttpPoster(Protocol):
    def post(self, url: str, json: dict, headers: dict, timeout: float): ...


def _default_poster() -> Any:
    import httpx

    return httpx.Client(follow_redirects=False)


_BEARER_RE = re.compile(r"ya29\.[A-Za-z0-9._\-]+")

_URGENCY_TITLE = {
    "urgent": "🚨 URGENT",
    "high": "⚠️ HIGH",
    "normal": "Notification",
    "low": "Notification",
}


def _build_secret_stripper(*secrets: str | None):
    real = [s for s in secrets if s]

    def _strip(text: str) -> str:
        if not text:
            return text
        for s in real:
            text = text.replace(s, "***")
        return _BEARER_RE.sub("ya29.***", text)

    return _strip


class FcmPushChannelAdapter:
    """Outbound HERALD adapter for Firebase Cloud Messaging HTTP v1."""

    name = "fcm-push"

    def __init__(
        self,
        *,
        project_id: str,
        access_token: str,
        poster: _HttpPoster | None = None,
        timeout: float = 10.0,
    ) -> None:
        self._project_id = project_id
        self._access_token = access_token
        self._poster = poster or _default_poster()
        self._timeout = timeout
        self._strip = _build_secret_stripper(access_token)
        self._endpoint = (
            f"https://fcm.googleapis.com/v1/projects/{project_id}/messages:send"
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
    ) -> FcmPushDispatchResult:
        title = _URGENCY_TITLE.get(priority, "Notification")
        message: dict[str, Any] = {
            "notification": {"title": title, "body": summary},
            "data": {
                "receipt_id": receipt_id,
                "classification": classification.value,
                "priority": priority,
            },
        }
        # A `/topics/...` recipient is a topic subscription; anything else
        # is a device registration token.
        if recipient.startswith("/topics/"):
            message["topic"] = recipient[len("/topics/") :]
        else:
            message["token"] = recipient

        headers = {
            "Authorization": f"Bearer {self._access_token}",
            "Content-Type": "application/json",
        }
        try:
            resp = self._poster.post(
                self._endpoint,
                json={"message": message},
                headers=headers,
                timeout=self._timeout,
            )
        except Exception as exc:  # noqa: BLE001 — network boundary
            return FcmPushDispatchResult(
                ok=False, error=self._strip(f"{type(exc).__name__}: {exc}")
            )

        if 200 <= resp.status_code < 300:
            data = _safe_json(resp)
            return FcmPushDispatchResult(
                ok=True,
                status_code=resp.status_code,
                message_id=data.get("name") if isinstance(data, dict) else None,
            )

        body = getattr(resp, "text", "") or ""
        return FcmPushDispatchResult(
            ok=False,
            status_code=resp.status_code,
            error=self._strip(f"HTTP {resp.status_code}: {body[:200]}"),
            reconnect_required=resp.status_code in (401, 403),
        )


def _safe_json(resp: Any) -> Any:
    try:
        return resp.json()
    except Exception:  # noqa: BLE001
        return {}


class FcmPushChannelAdapterProvider:
    """Factory; ``build()`` receives project id + OAuth2 access token."""

    name = "fcm-push"

    def capabilities(self) -> ChannelCapabilities:
        return ChannelCapabilities(
            name="fcm-push",
            direction=Direction.OUTBOUND,
            priority_levels=("low", "normal", "high", "urgent"),
            classification_ceiling=Classification.INTERNAL,
            supports_threading=False,
            supports_acknowledge=False,
            delivery_sla_p95_ms=2000,
            connector_ref="fcm-project",
        )

    def build(self, config: dict[str, Any] | None = None) -> FcmPushChannelAdapter:
        cfg = config or {}
        project_id = cfg.get("project_id")
        access_token = cfg.get("access_token")
        if not project_id:
            raise ValueError("fcm-push channel requires `project_id` in config")
        if not access_token:
            raise ValueError(
                "fcm-push channel requires `access_token` in config "
                "(OAuth2 bearer, resolved/refreshed via the secrets store)"
            )
        return FcmPushChannelAdapter(
            project_id=project_id,
            access_token=access_token,
            poster=cfg.get("poster"),
            timeout=cfg.get("timeout", 10.0),
        )


__all__ = [
    "FcmPushChannelAdapter",
    "FcmPushChannelAdapterProvider",
    "FcmPushDispatchResult",
]
