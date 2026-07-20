# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0

"""Azure Communication Services (ACS) email backend — raw REST.

Registered under the ``acs`` name in the email factory. Uses the ACS
Email REST API (``/emails:send``). Auth is either the connection-string
HMAC (default) or an Entra (Azure AD) bearer token
(``acs_endpoint`` + ``acs_access_token``). No Azure SDK dependency — the
signing lives in the shared ``channels/_acs.py`` helper so the base
install stays lean. The HTTP poster is injectable for offline tests.

The verified ``senderAddress`` is the channel-level ``from_address``
(ACS requires a domain-verified sender); a per-message override is
honored when present.
"""

from __future__ import annotations

import json as _json
import re
from typing import Any

from axiom.extensions.builtins.notifications.channels._acs import (
    parse_connection_string,
    rfc1123_now,
    sign_request,
)
from axiom.extensions.builtins.notifications.channels.email.base import (
    EmailMessage,
    EmailSendResult,
)
from axiom.extensions.builtins.notifications.channels.email.factory import (
    register_email_provider,
)

_ACS_EMAIL_API_VERSION = "2023-03-31"

# Redact base64-ish access keys that could echo through error text.
_ACCESSKEY_RE = re.compile(r"accesskey=[A-Za-z0-9+/=]+", re.IGNORECASE)

# The injected poster must expose ``post(url, *, content, headers, timeout)``
# (httpx.Client satisfies this).


def _default_poster() -> Any:
    import httpx

    return httpx.Client(follow_redirects=False)


class AcsEmailProvider:
    """Azure Communication Services email backend (REST ``/emails:send``)."""

    name = "acs"

    def __init__(
        self,
        *,
        endpoint: str,
        access_key: str | None = None,
        access_token: str | None = None,
        poster: Any | None = None,
        timeout: float = 15.0,
        clock: Any | None = None,
    ) -> None:
        if not access_key and not access_token:
            raise ValueError(
                "acs email backend requires an HMAC access key "
                "(from `acs_connection_string`) or an Entra `acs_access_token`"
            )
        self._endpoint = endpoint.rstrip("/")
        self._access_key = access_key
        self._access_token = access_token
        self._poster = poster or _default_poster()
        self._timeout = timeout
        self._clock = clock

    def _strip(self, text: str) -> str:
        if not text:
            return text
        if self._access_key:
            text = text.replace(self._access_key, "***")
        if self._access_token:
            text = text.replace(self._access_token, "***")
        return _ACCESSKEY_RE.sub("accesskey=***", text)

    def send(self, message: EmailMessage) -> EmailSendResult:
        url = (
            f"{self._endpoint}/emails:send"
            f"?api-version={_ACS_EMAIL_API_VERSION}"
        )
        content: dict[str, Any] = {"subject": message.subject}
        if message.body_text:
            content["plainText"] = message.body_text
        if message.body_html:
            content["html"] = message.body_html
        recipients: dict[str, Any] = {
            "to": [{"address": addr} for addr in message.to]
        }
        if message.cc:
            recipients["cc"] = [{"address": a} for a in message.cc]
        if message.bcc:
            recipients["bcc"] = [{"address": a} for a in message.bcc]
        body_obj: dict[str, Any] = {
            "senderAddress": message.from_address,
            "content": content,
            "recipients": recipients,
        }
        if message.reply_to:
            body_obj["replyTo"] = [{"address": message.reply_to}]

        body_bytes = _json.dumps(body_obj).encode("utf-8")
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

        try:
            resp = self._poster.post(
                url, content=body_bytes, headers=headers, timeout=self._timeout
            )
        except Exception as exc:  # noqa: BLE001 — network boundary
            return EmailSendResult(
                ok=False,
                provider=self.name,
                error=self._strip(f"{type(exc).__name__}: {exc}"),
            )

        # ACS returns 202 Accepted for a queued send; the operation id is
        # in the body (`id`) and/or the `Operation-Location` header.
        if 200 <= resp.status_code < 300:
            data = _safe_json(resp)
            op_id = data.get("id") if isinstance(data, dict) else None
            if not op_id:
                op_id = _get_header(resp, "Operation-Location")
            return EmailSendResult(
                ok=True,
                provider=self.name,
                status_code=resp.status_code,
                message_id=op_id,
            )

        body_text = getattr(resp, "text", "") or ""
        return EmailSendResult(
            ok=False,
            provider=self.name,
            status_code=resp.status_code,
            error=self._strip(f"HTTP {resp.status_code}: {body_text[:200]}"),
        )


def _safe_json(resp: Any) -> Any:
    try:
        return resp.json()
    except Exception:  # noqa: BLE001
        return {}


def _get_header(resp: Any, name: str) -> str | None:
    headers = getattr(resp, "headers", None)
    if headers is None:
        return None
    try:
        return headers.get(name)
    except AttributeError:
        return dict(headers).get(name)


def _build_acs(config: dict[str, Any]) -> AcsEmailProvider:
    """Builder registered with the email factory under ``acs``."""
    connection_string = config.get("acs_connection_string")
    endpoint = config.get("acs_endpoint") or config.get("acs_email_endpoint")
    access_key = None
    if connection_string:
        endpoint, access_key = parse_connection_string(connection_string)
    if not endpoint:
        raise ValueError(
            "acs email backend requires `acs_connection_string` or `acs_endpoint`"
        )
    return AcsEmailProvider(
        endpoint=endpoint,
        access_key=access_key,
        access_token=config.get("acs_access_token"),
        poster=config.get("poster"),
        timeout=float(config.get("timeout", 15.0)),
        clock=config.get("clock"),
    )


register_email_provider("acs", _build_acs)


__all__ = ["AcsEmailProvider"]
