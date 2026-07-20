# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0

"""Gmail API backend — ``users.messages.send`` via raw REST + OAuth2 bearer.

Registered under the ``gmail`` name in the email factory. Per the
connector pattern (fabric §5.3) we do NOT run the full 3-legged OAuth
dance here: the caller supplies either a ready OAuth2 **access token**
(``gmail_access_token``) or a **refresh token** plus client credentials
(``gmail_refresh_token`` + ``gmail_client_id`` + ``gmail_client_secret``)
that we exchange once for an access token via Google's token endpoint.
Both are resolved from the secrets store by the connector, not persisted
here.

No Google SDK dependency — the send is a single HTTPS POST, so the base
install stays lean. The HTTP poster is injectable for offline tests.
"""

from __future__ import annotations

import base64
import email.mime.text
import re
from typing import Any

from axiom.extensions.builtins.notifications.channels.email.base import (
    EmailMessage,
    EmailSendResult,
)
from axiom.extensions.builtins.notifications.channels.email.factory import (
    register_email_provider,
)

_GMAIL_SEND_ENDPOINT = (
    "https://gmail.googleapis.com/gmail/v1/users/me/messages/send"
)
_GOOGLE_TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"

# The injected poster must expose ``post(url, *, json|data, headers, timeout)``
# (httpx.Client satisfies this); it is used for both the message send
# (``json=``) and the one-shot refresh-token exchange (``data=``).


def _default_poster() -> Any:
    import httpx

    return httpx.Client(follow_redirects=False)


# Redact bearer/refresh secrets that could echo through error text.
_BEARER_RE = re.compile(r"ya29\.[A-Za-z0-9._\-]+")


def _build_secret_stripper(*secrets: str | None):
    real = [s for s in secrets if s]

    def _strip(text: str) -> str:
        if not text:
            return text
        for s in real:
            text = text.replace(s, "***")
        return _BEARER_RE.sub("ya29.***", text)

    return _strip


def _build_raw(message: EmailMessage) -> str:
    """Render an ``EmailMessage`` to a base64url RFC-2822 blob for Gmail.

    Gmail's ``raw`` field is a URL-safe base64 of the full MIME message.
    HTML-only messages send as ``text/html``; otherwise ``text/plain``.
    """
    if message.body_html and not message.body_text:
        mime = email.mime.text.MIMEText(message.body_html, "html", "utf-8")
    else:
        mime = email.mime.text.MIMEText(
            message.body_text or "", "plain", "utf-8"
        )
    mime["Subject"] = message.subject
    mime["From"] = (
        f'"{message.from_name}" <{message.from_address}>'
        if message.from_name
        else message.from_address
    )
    mime["To"] = ", ".join(message.to)
    if message.cc:
        mime["Cc"] = ", ".join(message.cc)
    if message.reply_to:
        mime["Reply-To"] = message.reply_to
    for k, v in message.headers.items():
        mime[k] = v
    return base64.urlsafe_b64encode(mime.as_bytes()).decode("ascii")


class GmailEmailProvider:
    """Gmail API outbound backend (``users.messages.send``)."""

    name = "gmail"

    def __init__(
        self,
        *,
        access_token: str | None = None,
        refresh_token: str | None = None,
        client_id: str | None = None,
        client_secret: str | None = None,
        poster: Any | None = None,
        timeout: float = 10.0,
    ) -> None:
        if not access_token and not (
            refresh_token and client_id and client_secret
        ):
            raise ValueError(
                "gmail backend requires `gmail_access_token`, or "
                "`gmail_refresh_token` + `gmail_client_id` + "
                "`gmail_client_secret` to mint one"
            )
        self._access_token = access_token
        self._refresh_token = refresh_token
        self._client_id = client_id
        self._client_secret = client_secret
        self._poster = poster or _default_poster()
        self._timeout = timeout
        self._strip = _build_secret_stripper(
            access_token, refresh_token, client_secret
        )

    def _bearer(self) -> str:
        if self._access_token:
            return self._access_token
        # Single refresh-token exchange (NOT the full 3-legged dance).
        resp = self._poster.post(
            _GOOGLE_TOKEN_ENDPOINT,
            data={
                "grant_type": "refresh_token",
                "refresh_token": self._refresh_token,
                "client_id": self._client_id,
                "client_secret": self._client_secret,
            },
            timeout=self._timeout,
        )
        if not (200 <= resp.status_code < 300):
            body = getattr(resp, "text", "") or ""
            raise RuntimeError(
                self._strip(f"token refresh HTTP {resp.status_code}: {body[:200]}")
            )
        data = _safe_json(resp)
        token = data.get("access_token") if isinstance(data, dict) else None
        if not token:
            raise RuntimeError("token refresh returned no access_token")
        # Cache for the life of this adapter instance.
        self._access_token = token
        return token

    def send(self, message: EmailMessage) -> EmailSendResult:
        try:
            bearer = self._bearer()
        except Exception as exc:  # noqa: BLE001 — token acquisition boundary
            return EmailSendResult(
                ok=False,
                provider=self.name,
                error=self._strip(f"{type(exc).__name__}: {exc}"),
            )

        payload = {"raw": _build_raw(message)}
        headers = {
            "Authorization": f"Bearer {bearer}",
            "Content-Type": "application/json",
        }
        try:
            resp = self._poster.post(
                _GMAIL_SEND_ENDPOINT,
                json=payload,
                headers=headers,
                timeout=self._timeout,
            )
        except Exception as exc:  # noqa: BLE001 — network boundary
            return EmailSendResult(
                ok=False,
                provider=self.name,
                error=self._strip(f"{type(exc).__name__}: {exc}"),
            )

        if 200 <= resp.status_code < 300:
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
            error=self._strip(f"HTTP {resp.status_code}: {body[:200]}"),
        )


def _safe_json(resp: Any) -> Any:
    try:
        return resp.json()
    except Exception:  # noqa: BLE001
        return {}


def _build_gmail(config: dict[str, Any]) -> GmailEmailProvider:
    return GmailEmailProvider(
        access_token=config.get("gmail_access_token"),
        refresh_token=config.get("gmail_refresh_token"),
        client_id=config.get("gmail_client_id"),
        client_secret=config.get("gmail_client_secret"),
        poster=config.get("poster"),
        timeout=float(config.get("timeout", 10.0)),
    )


register_email_provider("gmail", _build_gmail)


__all__ = ["GmailEmailProvider"]
