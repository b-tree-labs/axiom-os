# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Email backend Protocol + value objects.

The HERALD ``email`` channel is a nested Factory/Provider: a single
``EmailChannelAdapter`` fronts the channel registry, and underneath an
``EmailProvider`` Protocol fronts every cloud email backend (SMTP,
Microsoft 365 Graph, Gmail API, AWS SES, SendGrid, Postmark, Resend,
Mailgun, Mailtrap, …).

Why nested:

- HERALD's channel registry (spec §4) treats ``email`` as one admitted
  channel — operators reason about "send to email" once, not per vendor.
- Operators / orgs have strong opinions about the email vendor; we will
  not predict the install base. A subpackage of provider files, each
  registering itself at import time, accepts that reality without code
  changes when a new vendor lands.
- OAuth-driven providers (M365 Graph, Gmail API) need KEEP cap-token
  handoff per fabric §5.3; API-key providers (Resend / Postmark / SES /
  SendGrid / Mailgun) need only a single secret. The Protocol hides the
  difference behind ``send(message)``.

Strict minimum viable surface: build an ``EmailMessage``, hand it to a
provider, get back an ``EmailSendResult``. Everything else (threading,
attachments, inline images, deliverability hooks, IMAP poll for reply
ingest) lands in HERALD-2b.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable


@dataclass(frozen=True)
class EmailMessage:
    """Provider-agnostic outbound message shape.

    Body fields are alternatives, not both required. When both are set
    the provider sends a multipart/alternative; when only one is set the
    provider sends that one.
    """

    to: tuple[str, ...]
    """Recipient addresses. Tuple so the message is hashable + immutable."""

    subject: str

    from_address: str
    """Sender. RFC-5321 envelope sender; providers may override the
    visible From if their account binds it (e.g. Resend domain-locked)."""

    body_text: str | None = None
    body_html: str | None = None

    from_name: str = ""
    """Optional display name; rendered as ``From: "Name" <addr>``."""

    cc: tuple[str, ...] = ()
    bcc: tuple[str, ...] = ()
    reply_to: str = ""
    headers: dict[str, str] = field(default_factory=dict)
    """Extra RFC-5322 headers. Use for X-Priority, In-Reply-To, References,
    or vendor-custom routing hints."""

    def __post_init__(self) -> None:
        if not self.to:
            raise ValueError("EmailMessage requires at least one `to` recipient")
        if not self.body_text and not self.body_html:
            raise ValueError(
                "EmailMessage requires `body_text` or `body_html` (or both)"
            )


@dataclass
class EmailSendResult:
    """One send outcome. Mirrors ``SlackDispatchResult`` shape."""

    ok: bool
    provider: str = ""
    message_id: str | None = None
    """Vendor-assigned id (Resend's `id`, SES `MessageId`, SMTP queue id).
    The HERALD receipt indexes by this so reply ingest can thread."""
    error: str | None = None
    status_code: int | None = None


@runtime_checkable
class EmailProvider(Protocol):
    """Backend Protocol every cloud email vendor adapter implements.

    ``name`` is the vendor's registered key (``smtp``, ``resend``,
    ``microsoft365``, ``gmail``, ``ses``, …). It is the lookup key in
    the registry + the value stamped into ``EmailSendResult.provider``.
    """

    name: str

    def send(self, message: EmailMessage) -> EmailSendResult: ...


__all__ = [
    "EmailMessage",
    "EmailProvider",
    "EmailSendResult",
]
