# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0


# DEPRECATED 2026-06-01 (v0.30) per ADR-059 + ADR-060: publishing emits
# events on the EventBus; HERALD's agent_bridge routes them through the
# connector adapters. This module survives only for backwards-compat
# during the v0.30 transition and is REMOVED IN v0.31.
"""SMTPNotificationProvider — send email via SMTP.

Sends notifications as plain-text or HTML email. Supports markdown-to-HTML
conversion for rich email bodies.

Configuration (via .publisher.yaml or constructor):
    smtp_host:     SMTP server hostname (default: localhost)
    smtp_port:     SMTP server port (default: 587)
    smtp_user:     SMTP auth username (optional)
    smtp_password: SMTP auth password (optional)
    smtp_use_tls:  Use STARTTLS (default: true)
    from_address:  Sender email address (required)
    from_name:     Sender display name (optional)
"""

from __future__ import annotations

import email.mime.multipart
import email.mime.text
import smtplib
import ssl
from typing import Any

from ...factory import PublisherFactory
from ..base import NotificationProvider


def _markdown_to_html(md_text: str) -> str:
    """Best-effort markdown to HTML. Falls back to <pre> if no converter."""
    try:
        import markdown  # type: ignore
        return markdown.markdown(md_text, extensions=["extra", "sane_lists"])
    except ImportError:
        pass
    # Minimal fallback: wrap paragraphs, bold, and lists
    lines = md_text.split("\n")
    html_parts = []
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("# "):
            html_parts.append(f"<h1>{stripped[2:]}</h1>")
        elif stripped.startswith("## "):
            html_parts.append(f"<h2>{stripped[3:]}</h2>")
        elif stripped.startswith("### "):
            html_parts.append(f"<h3>{stripped[4:]}</h3>")
        elif stripped.startswith("- "):
            html_parts.append(f"<li>{stripped[2:]}</li>")
        elif stripped.startswith("**") and stripped.endswith("**"):
            html_parts.append(f"<p><strong>{stripped[2:-2]}</strong></p>")
        elif stripped:
            # Handle inline bold
            import re
            processed = re.sub(r'\*\*(.+?)\*\*', r'<strong>\1</strong>', stripped)
            html_parts.append(f"<p>{processed}</p>")
        else:
            html_parts.append("")
    return "\n".join(html_parts)


class SMTPNotificationProvider(NotificationProvider):
    """Send notifications as email via SMTP."""

    def __init__(self, config: dict[str, Any] | None = None):
        config = config or {}
        self.smtp_host = config.get("smtp_host", "localhost")
        self.smtp_port = int(config.get("smtp_port", 587))
        self.smtp_user = config.get("smtp_user", "")
        self.smtp_password = config.get("smtp_password", "")
        self.use_tls = config.get("smtp_use_tls", True)
        self.from_address = config.get("from_address", "")
        self.from_name = config.get("from_name", "")

    def send(
        self,
        recipients: list[str],
        subject: str,
        body: str,
        urgency: str = "normal",
    ) -> bool:
        """Send an email notification.

        Args:
            recipients: List of email addresses.
            subject: Email subject line.
            body: Email body in markdown format — converted to HTML.
            urgency: "low", "normal", "high" — maps to X-Priority header.
        """
        if not recipients:
            return False
        if not self.from_address:
            raise ValueError("from_address is required for SMTP provider")

        # Build message
        msg = email.mime.multipart.MIMEMultipart("alternative")
        sender = f"{self.from_name} <{self.from_address}>" if self.from_name else self.from_address
        msg["From"] = sender
        msg["To"] = ", ".join(recipients)
        msg["Subject"] = subject

        # Priority headers
        priority_map = {"low": "5", "normal": "3", "high": "1"}
        msg["X-Priority"] = priority_map.get(urgency, "3")

        # Plain text part
        msg.attach(email.mime.text.MIMEText(body, "plain", "utf-8"))

        # HTML part
        html_body = _markdown_to_html(body)
        html_content = (
            "<html><body style=\"font-family: -apple-system, BlinkMacSystemFont, "
            "'Segoe UI', Roboto, sans-serif; line-height: 1.6; color: #333;\">"
            f"{html_body}</body></html>"
        )
        msg.attach(email.mime.text.MIMEText(html_content, "html", "utf-8"))

        # Send
        try:
            if self.use_tls:
                context = ssl.create_default_context()
                server = smtplib.SMTP(self.smtp_host, self.smtp_port, timeout=30)
                server.starttls(context=context)
            else:
                server = smtplib.SMTP(self.smtp_host, self.smtp_port, timeout=30)

            if self.smtp_user and self.smtp_password:
                server.login(self.smtp_user, self.smtp_password)

            server.sendmail(self.from_address, recipients, msg.as_string())
            server.quit()
            return True
        except Exception as e:
            print(f"SMTP send failed: {e}")
            return False


# Self-register with factory
PublisherFactory.register("notification", "smtp", SMTPNotificationProvider)
