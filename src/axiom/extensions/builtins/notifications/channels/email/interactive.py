# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0

"""Email as a bidirectional ``InteractiveChannel`` (ADR-074, B4).

The fourth transport — and the final generalization check: the DT gate +
control plane run over email too. Outbound reuses the nested email provider
factory (SMTP/Resend/…); inbound reply-ingest arrives via the same
``InboundReceiver`` seam as SMS. Approvals degrade to reply-keywords; threading
follows ``In-Reply-To``/``References`` headers. ``parse_email_inbound`` is pure.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any

from ..interactive import (
    ActionHandler,
    ApprovalOutcome,
    ApprovalRequest,
    ChannelMessage,
    MessageHandler,
)

_YES = {"yes", "y", "confirm", "approve", "ok"}
_NO = {"no", "n", "reject", "deny"}
# A reply quotes the prior thread under a "On … wrote:" line; keep only the top.
_QUOTE = re.compile(r"\n[>\s]*On .*wrote:.*\Z", re.DOTALL)


def _first_meaningful_line(body: str) -> str:
    body = _QUOTE.sub("", body)
    for line in body.splitlines():
        if line.strip() and not line.lstrip().startswith(">"):
            return line.strip()
    return body.strip()


def parse_email_inbound(payload: dict) -> ChannelMessage | ApprovalOutcome | None:
    """Map an inbound email payload (``from``, ``body``, ``in_reply_to``) to a
    vendor-neutral type. The first non-quoted line decides YES/NO/number/text."""
    body = payload.get("body") or payload.get("text") or ""
    frm = payload.get("from") or "unknown"
    thread = payload.get("in_reply_to") or payload.get("references") or None
    first = _first_meaningful_line(body)
    if not first:
        return None
    # Match on the first word so "No, not yet" / "Yes!" resolve cleanly.
    low = re.split(r"[\s,.!]+", first.lower(), maxsplit=1)[0]
    if low in _YES:
        return ApprovalOutcome(action_id="confirm", actor=frm, thread_id=thread)
    if low in _NO:
        return ApprovalOutcome(action_id="reject", actor=frm, thread_id=thread)
    return ChannelMessage(text=first, author=frm, thread_id=thread, is_agent=False)


class EmailInteractiveChannel:
    """Bidirectional email channel. Implements ``InteractiveChannel``.

    ``send`` is injectable ``(to, subject, body, thread_id) -> Any``; the default
    wraps the email provider factory. Inbound is driven by ``dispatch(payload)``
    from the webhook/IMAP receiver — no ``run()`` loop."""

    def __init__(
        self,
        *,
        to_address: str,
        from_address: str = "",
        subject: str = "Axi",
        provider_config: dict | None = None,
        send: Callable[..., Any] | None = None,
    ) -> None:
        self._to = to_address
        self._from = from_address
        self._subject = subject
        self._send = send or self._default_send(provider_config or {})
        self._msg_handlers: list[MessageHandler] = []
        self._action_handlers: list[ActionHandler] = []

    def _default_send(self, config: dict):  # pragma: no cover - needs a configured provider
        from .base import EmailMessage
        from .factory import detect_email_provider

        provider = detect_email_provider(config)
        if provider is None:
            raise ValueError("no email provider configured (see notifications email factory)")

        def _send(to: str, subject: str, body: str, thread_id: str | None):
            headers = {"In-Reply-To": thread_id} if thread_id else {}
            return provider.send(EmailMessage(
                to=(to,), subject=subject, from_address=self._from,
                body_text=body, headers=headers,
            ))

        return _send

    def post(self, text: str, *, thread_id: str | None = None, author: str = "agent",
             icon_url: str | None = None) -> str:
        subject = self._subject if not author or author == "agent" else f"{self._subject} · {author}"
        self._send(self._to, subject, text, thread_id)
        return thread_id or self._to

    def request_approval(self, request: ApprovalRequest) -> str:
        body = f"{request.prompt}\n\nReply YES to confirm, NO to reject, or send the measured value."
        self._send(self._to, self._subject, body, request.thread_id)
        return request.thread_id or self._to

    def on_message(self, handler: MessageHandler) -> None:
        self._msg_handlers.append(handler)

    def on_action(self, handler: ActionHandler) -> None:
        self._action_handlers.append(handler)

    def dispatch(self, payload: dict) -> None:
        parsed = parse_email_inbound(payload)
        if isinstance(parsed, ChannelMessage):
            for h in list(self._msg_handlers):
                h(parsed)
        elif isinstance(parsed, ApprovalOutcome):
            for h in list(self._action_handlers):
                h(parsed)


# descriptor env var (UPPER) → email-factory config key (lower) it discovers on.
_ENV_TO_PROVIDER_CFG = {
    "SMTP_HOST": "smtp_host",
    "SMTP_PASSWORD": "smtp_password",
    "SMTP_USER": "smtp_user",
    "SMTP_PORT": "smtp_port",
    "RESEND_API_KEY": "resend_api_key",
}


def make_email_channel(*, env: dict) -> EmailInteractiveChannel:
    """Factory for the connector resolver (ADR-074 ``provider_entry``). Maps the
    descriptor's UPPER env vars to the email factory's lowercase config keys."""
    cfg = {dst: env[src] for src, dst in _ENV_TO_PROVIDER_CFG.items() if env.get(src)}
    return EmailInteractiveChannel(
        to_address=env["EMAIL_TO"],
        from_address=env.get("EMAIL_FROM", ""),
        provider_config=cfg,
    )


__all__ = ["EmailInteractiveChannel", "parse_email_inbound", "make_email_channel"]
