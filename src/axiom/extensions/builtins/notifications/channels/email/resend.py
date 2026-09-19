# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Resend backend — modern API-based email vendor.

Resend (resend.com) is the lightest API-key-only provider in the
cluster: a single token + a POST to ``/emails`` is enough. Picked as
the second backend (after SMTP) so the API-key path is exercised before
the OAuth-bound vendors (Microsoft 365 Graph, Gmail) land in HERALD-2b.

The HTTP poster is injectable so tests stay offline + deterministic.
"""

from __future__ import annotations

import re
from typing import Any, Protocol

from axiom.extensions.builtins.notifications.channels.email.base import (
    EmailMessage,
    EmailSendResult,
)
from axiom.extensions.builtins.notifications.channels.email.factory import (
    register_email_provider,
)

_RESEND_ENDPOINT = "https://api.resend.com/emails"


class _HttpPoster(Protocol):
    def post(self, url: str, json: dict, headers: dict, timeout: float): ...


def _default_poster() -> _HttpPoster:
    import httpx

    return httpx.Client(follow_redirects=False)


# Match any ``re_`` Resend-key prefix in error text and redact.
_RESEND_KEY_RE = re.compile(r"re_[A-Za-z0-9_]+")


def _strip_key(text: str) -> str:
    return _RESEND_KEY_RE.sub("re_***", text)


class ResendEmailProvider:
    """Resend (resend.com) outbound."""

    name = "resend"

    def __init__(
        self,
        *,
        api_key: str,
        poster: _HttpPoster | None = None,
        timeout: float = 10.0,
    ) -> None:
        self._api_key = api_key
        self._poster = poster or _default_poster()
        self._timeout = timeout

    def send(self, message: EmailMessage) -> EmailSendResult:
        payload: dict[str, Any] = {
            "from": (
                f"{message.from_name} <{message.from_address}>"
                if message.from_name
                else message.from_address
            ),
            "to": list(message.to),
            "subject": message.subject,
        }
        if message.body_text:
            payload["text"] = message.body_text
        if message.body_html:
            payload["html"] = message.body_html
        if message.cc:
            payload["cc"] = list(message.cc)
        if message.bcc:
            payload["bcc"] = list(message.bcc)
        if message.reply_to:
            payload["reply_to"] = message.reply_to
        if message.headers:
            payload["headers"] = dict(message.headers)

        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }
        try:
            resp = self._poster.post(
                _RESEND_ENDPOINT,
                json=payload,
                headers=headers,
                timeout=self._timeout,
            )
        except Exception as exc:  # noqa: BLE001
            return EmailSendResult(
                ok=False,
                provider=self.name,
                error=_strip_key(f"{type(exc).__name__}: {exc}"),
            )

        if 200 <= resp.status_code < 300:
            # Resend returns ``{"id": "abc-uuid"}`` on success.
            data = _safe_json(resp)
            return EmailSendResult(
                ok=True,
                provider=self.name,
                status_code=resp.status_code,
                message_id=data.get("id") if isinstance(data, dict) else None,
            )

        body = getattr(resp, "text", "") or ""
        return EmailSendResult(
            ok=False,
            provider=self.name,
            status_code=resp.status_code,
            error=_strip_key(f"HTTP {resp.status_code}: {body[:200]}"),
        )


def _safe_json(resp: Any) -> Any:
    try:
        return resp.json()
    except Exception:  # noqa: BLE001
        return {}


def _build_resend(config: dict[str, Any]) -> ResendEmailProvider:
    return ResendEmailProvider(
        api_key=config["resend_api_key"],
        poster=config.get("poster"),
        timeout=float(config.get("timeout", 10.0)),
    )


register_email_provider("resend", _build_resend)


__all__ = ["ResendEmailProvider"]
