# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""HERALD ``email`` ChannelAdapter — wraps any ``EmailProvider``.

The outer Provider/Factory layer (``ChannelAdapterRegistry``) sees one
channel: ``email``. The inner Provider/Factory layer
(``EmailProvider`` + ``detect_email_provider``) picks the vendor
backend from config. Operators reason about "send to email" once;
they pick the vendor in config.

Per spec §9 the channel ceiling is ``INTERNAL``. Threading via
``In-Reply-To`` / ``References`` headers is wire-supported even though
HERALD-2a outbound doesn't emit them yet (the receipt threading lands
in HERALD-2b alongside IMAP reply ingest).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from axiom.extensions.builtins.notifications.sender import SenderIdentity

from dataclasses import dataclass
from typing import Any

from axiom.extensions.builtins.notifications.channels.base import (
    ChannelCapabilities,
    Direction,
)
from axiom.extensions.builtins.notifications.channels.email.base import (
    EmailMessage,
    EmailProvider,
    EmailSendResult,
)
from axiom.extensions.builtins.notifications.channels.email.factory import (
    detect_email_provider,
)
from axiom.governance import Classification


@dataclass
class EmailDispatchResult:
    """Channel-adapter result. Mirrors ``InboxDispatchResult``; carries
    the underlying provider's outcome for receipt indexing."""

    ok: bool
    provider: str = ""
    message_id: str | None = None
    error: str | None = None
    status_code: int | None = None


_URGENCY_SUBJECT_PREFIX = {
    "urgent": "[URGENT] ",
    "high": "[HIGH] ",
    "normal": "",
    "low": "",
}


class EmailChannelAdapter:
    """HERALD outbound ChannelAdapter wrapping an ``EmailProvider``.

    Construction parameters:

    - ``provider``: the chosen backend (SMTP / Resend / SES / …).
    - ``from_address``: required at adapter level so every send under
      this channel has a stamped envelope sender. Per-message override
      via ``message.from_address`` is allowed but rare.
    - ``from_name``: optional display name.
    """

    name = "email"

    def __init__(
        self,
        *,
        provider: EmailProvider,
        from_address: str,
        from_name: str = "",
    ) -> None:
        if not from_address:
            raise ValueError("EmailChannelAdapter requires `from_address`")
        self._provider = provider
        self._from_address = from_address
        self._from_name = from_name

    def deliver_sync(
        self,
        *,
        recipient: str,
        receipt_id: str,
        classification: Classification,
        priority: str,
        summary: str,
        body_text: str | None = None,
        body_html: str | None = None,
        sender: SenderIdentity | None = None,
    ) -> EmailDispatchResult:
        subject_prefix = _URGENCY_SUBJECT_PREFIX.get(priority, "")
        text = body_text or _default_body(
            summary=summary,
            recipient=recipient,
            classification=classification,
            priority=priority,
            receipt_id=receipt_id,
        )
        _from_name = self._from_name
        _from_address = self._from_address
        if sender is not None:
            from axiom.extensions.builtins.notifications.sender import render_for_channel
            _rs = render_for_channel(sender, "email")
            _from_name = _rs.display or _from_name
            if _rs.from_address:
                _from_address = _rs.from_address
        message = EmailMessage(
            to=(recipient,),
            subject=f"{subject_prefix}{summary}",
            from_address=_from_address,
            from_name=_from_name,
            body_text=text,
            body_html=body_html,
            headers={
                "X-Axiom-Receipt-Id": receipt_id,
                "X-Axiom-Classification": classification.value,
                "X-Axiom-Priority": priority,
            },
        )

        result: EmailSendResult = self._provider.send(message)
        return EmailDispatchResult(
            ok=result.ok,
            provider=result.provider or self._provider.name,
            message_id=result.message_id,
            error=result.error,
            status_code=result.status_code,
        )


def _default_body(
    *,
    summary: str,
    recipient: str,
    classification: Classification,
    priority: str,
    receipt_id: str,
) -> str:
    return (
        f"{summary}\n\n"
        f"-- \n"
        f"to: {recipient}\n"
        f"priority: {priority}\n"
        f"classification: {classification.value}\n"
        f"receipt: {receipt_id}\n"
    )


class EmailChannelAdapterProvider:
    """HERALD ChannelAdapterProvider. Picks the inner EmailProvider via
    ``detect_email_provider`` over the supplied config dict.

    Required config:
      - ``from_address`` (channel-level sender)
    Plus one of:
      - ``smtp_host`` (+ optional smtp_user/smtp_password/...)  → SMTP
      - ``resend_api_key``                                       → Resend
      - ``provider``: explicit override + the matching keys
    """

    name = "email"

    def capabilities(self) -> ChannelCapabilities:
        return ChannelCapabilities(
            name="email",
            direction=Direction.OUTBOUND,
            priority_levels=("low", "normal", "high", "urgent"),
            classification_ceiling=Classification.INTERNAL,
            supports_threading=True,
            supports_acknowledge=False,
            delivery_sla_p95_ms=5000,
            connector_ref="email",
        )

    def build(self, config: dict[str, Any] | None = None) -> EmailChannelAdapter:
        cfg = config or {}
        from_address = cfg.get("from_address")
        if not from_address:
            raise ValueError(
                "email channel requires `from_address` in config"
            )
        provider = detect_email_provider(cfg)
        if provider is None:
            raise ValueError(
                "email channel requires backend config — one of: "
                "smtp_host, resend_api_key, sendgrid_api_key, "
                "postmark_server_token, mailgun_api_key, "
                "ses_access_key_id, microsoft365_client_id, gmail_client_id"
            )
        return EmailChannelAdapter(
            provider=provider,
            from_address=from_address,
            from_name=cfg.get("from_name", ""),
        )


__all__ = [
    "EmailChannelAdapter",
    "EmailChannelAdapterProvider",
    "EmailDispatchResult",
]
