# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""SMTP backend — universal email fallback.

The cloud-vendor adapters (Resend, SendGrid, SES, M365 Graph, Gmail API,
Postmark, Mailgun) cover the common cases; ``smtp`` exists so any host
with a postfix / sendgrid-smtp / mailtrap-smtp endpoint can send without
a new adapter. Auth via STARTTLS + login is the default; plain (no
auth) is supported for local relays.

The transport (``smtplib.SMTP``) is injectable so tests stay offline
and deterministic.
"""

from __future__ import annotations

import email.mime.multipart
import email.mime.text
import smtplib
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from axiom.extensions.builtins.notifications.channels.email.base import (
    EmailMessage,
    EmailSendResult,
)
from axiom.extensions.builtins.notifications.channels.email.factory import (
    register_email_provider,
)


@dataclass
class SmtpConfig:
    host: str
    port: int = 587
    username: str = ""
    password: str = ""
    use_tls: bool = True
    timeout: float = 30.0


class SmtpEmailProvider:
    """SMTP-via-smtplib outbound. Auth optional (open relays + dev MX
    servers like mailpit / mailtrap use plain ``smtplib.SMTP`` without
    AUTH)."""

    name = "smtp"

    def __init__(
        self,
        config: SmtpConfig,
        *,
        smtp_factory: Callable[..., smtplib.SMTP] | None = None,
    ) -> None:
        self._config = config
        self._smtp_factory = smtp_factory or smtplib.SMTP

    def send(self, message: EmailMessage) -> EmailSendResult:
        cfg = self._config
        try:
            client = self._smtp_factory(cfg.host, cfg.port, timeout=cfg.timeout)
        except Exception as exc:  # noqa: BLE001
            return EmailSendResult(
                ok=False,
                provider=self.name,
                error=f"{type(exc).__name__}: {exc}",
            )

        try:
            if cfg.use_tls:
                client.starttls()
            if cfg.username and cfg.password:
                client.login(cfg.username, cfg.password)
            mime = _build_mime(message)
            recipients = list(message.to) + list(message.cc) + list(message.bcc)
            client.sendmail(message.from_address, recipients, mime.as_string())
            return EmailSendResult(ok=True, provider=self.name)
        except Exception as exc:  # noqa: BLE001
            return EmailSendResult(
                ok=False,
                provider=self.name,
                error=f"{type(exc).__name__}: {exc}",
            )
        finally:
            try:
                client.quit()
            except Exception:  # noqa: BLE001
                pass


def _build_mime(message: EmailMessage) -> email.mime.multipart.MIMEMultipart:
    """Render an ``EmailMessage`` as a MIMEMultipart message."""
    msg = email.mime.multipart.MIMEMultipart("alternative")
    msg["Subject"] = message.subject
    msg["From"] = (
        f'"{message.from_name}" <{message.from_address}>'
        if message.from_name
        else message.from_address
    )
    msg["To"] = ", ".join(message.to)
    if message.cc:
        msg["Cc"] = ", ".join(message.cc)
    if message.reply_to:
        msg["Reply-To"] = message.reply_to
    for k, v in message.headers.items():
        msg[k] = v
    # text/plain first per RFC-2046 §5.1.4 (clients pick the last part
    # they can render).
    if message.body_text:
        msg.attach(email.mime.text.MIMEText(message.body_text, "plain", "utf-8"))
    if message.body_html:
        msg.attach(email.mime.text.MIMEText(message.body_html, "html", "utf-8"))
    return msg


def _build_smtp(config: dict[str, Any]) -> SmtpEmailProvider:
    """Builder registered with the factory."""
    return SmtpEmailProvider(
        SmtpConfig(
            host=config["smtp_host"],
            port=int(config.get("smtp_port", 587)),
            username=config.get("smtp_user", ""),
            password=config.get("smtp_password", ""),
            use_tls=bool(config.get("smtp_use_tls", True)),
            timeout=float(config.get("smtp_timeout", 30.0)),
        ),
        smtp_factory=config.get("smtp_factory"),
    )


register_email_provider("smtp", _build_smtp)


__all__ = [
    "SmtpConfig",
    "SmtpEmailProvider",
]
