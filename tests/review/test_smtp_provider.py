# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tests for axiom.extensions.builtins.publishing.providers.notification.smtp — SMTP email provider."""

from unittest.mock import MagicMock, patch

import pytest

from axiom.extensions.builtins.publishing.factory import PublisherFactory
from axiom.extensions.builtins.publishing.providers.notification.smtp import (
    SMTPNotificationProvider,
    _markdown_to_html,
)

# ── _markdown_to_html ────────────────────────────────────────────────

class TestMarkdownToHtml:
    def test_headings(self):
        html = _markdown_to_html("# Title\n\n## Subtitle\n\n### Sub-sub")
        assert "<h1>Title</h1>" in html
        assert "<h2>Subtitle</h2>" in html
        assert "<h3>Sub-sub</h3>" in html

    def test_list_items(self):
        html = _markdown_to_html("- Item one\n- Item two")
        assert "<li>Item one</li>" in html
        assert "<li>Item two</li>" in html

    def test_bold_text(self):
        html = _markdown_to_html("This has **bold** words.")
        assert "<strong>bold</strong>" in html

    def test_plain_paragraph(self):
        html = _markdown_to_html("Just a paragraph.")
        assert "<p>Just a paragraph.</p>" in html

    def test_empty_input(self):
        html = _markdown_to_html("")
        assert html is not None


# ── SMTPNotificationProvider ─────────────────────────────────────────

class TestSMTPProvider:
    def test_init_defaults(self):
        provider = SMTPNotificationProvider()
        assert provider.smtp_host == "localhost"
        assert provider.smtp_port == 587
        assert provider.use_tls is True

    def test_init_from_config(self):
        config = {
            "smtp_host": "mail.example.com",
            "smtp_port": "465",
            "smtp_user": "user",
            "smtp_password": "pass",
            "smtp_use_tls": False,
            "from_address": "noreply@example.com",
            "from_name": "Neut Bot",
        }
        provider = SMTPNotificationProvider(config)
        assert provider.smtp_host == "mail.example.com"
        assert provider.smtp_port == 465
        assert provider.from_address == "noreply@example.com"
        assert provider.from_name == "Neut Bot"

    def test_send_requires_from_address(self):
        provider = SMTPNotificationProvider({"from_address": ""})
        with pytest.raises(ValueError, match="from_address"):
            provider.send(["user@example.com"], "Subject", "Body")

    def test_send_empty_recipients(self):
        provider = SMTPNotificationProvider({"from_address": "noreply@example.com"})
        result = provider.send([], "Subject", "Body")
        assert result is False

    @patch("axiom.extensions.builtins.publishing.providers.notification.smtp.smtplib.SMTP")
    def test_send_success(self, mock_smtp_class):
        mock_server = MagicMock()
        mock_smtp_class.return_value = mock_server

        provider = SMTPNotificationProvider({
            "from_address": "sender@example.com",
            "smtp_use_tls": False,
        })
        result = provider.send(
            recipients=["user@example.com"],
            subject="Test Subject",
            body="**Hello** from Neut.",
        )

        assert result is True
        mock_server.sendmail.assert_called_once()
        call_args = mock_server.sendmail.call_args
        assert call_args[0][0] == "sender@example.com"
        assert call_args[0][1] == ["user@example.com"]
        # Verify message contains subject
        msg_str = call_args[0][2]
        assert "Test Subject" in msg_str

    @patch("axiom.extensions.builtins.publishing.providers.notification.smtp.smtplib.SMTP")
    def test_send_with_tls(self, mock_smtp_class):
        mock_server = MagicMock()
        mock_smtp_class.return_value = mock_server

        provider = SMTPNotificationProvider({
            "from_address": "sender@example.com",
            "smtp_use_tls": True,
        })
        provider.send(["user@example.com"], "Subject", "Body")

        mock_server.starttls.assert_called_once()

    @patch("axiom.extensions.builtins.publishing.providers.notification.smtp.smtplib.SMTP")
    def test_send_with_auth(self, mock_smtp_class):
        mock_server = MagicMock()
        mock_smtp_class.return_value = mock_server

        provider = SMTPNotificationProvider({
            "from_address": "sender@example.com",
            "smtp_user": "myuser",
            "smtp_password": "mypass",
            "smtp_use_tls": False,
        })
        provider.send(["user@example.com"], "Subject", "Body")

        mock_server.login.assert_called_once_with("myuser", "mypass")

    @patch("axiom.extensions.builtins.publishing.providers.notification.smtp.smtplib.SMTP")
    def test_send_priority_header(self, mock_smtp_class):
        mock_server = MagicMock()
        mock_smtp_class.return_value = mock_server

        provider = SMTPNotificationProvider({
            "from_address": "sender@example.com",
            "smtp_use_tls": False,
        })
        provider.send(["user@example.com"], "Urgent", "Body", urgency="high")

        msg_str = mock_server.sendmail.call_args[0][2]
        assert "X-Priority: 1" in msg_str

    @patch("axiom.extensions.builtins.publishing.providers.notification.smtp.smtplib.SMTP")
    def test_send_smtp_failure(self, mock_smtp_class):
        mock_smtp_class.side_effect = ConnectionRefusedError("Connection refused")

        provider = SMTPNotificationProvider({
            "from_address": "sender@example.com",
            "smtp_use_tls": False,
        })
        result = provider.send(["user@example.com"], "Subject", "Body")
        assert result is False

    @patch("axiom.extensions.builtins.publishing.providers.notification.smtp.smtplib.SMTP")
    def test_send_from_name(self, mock_smtp_class):
        mock_server = MagicMock()
        mock_smtp_class.return_value = mock_server

        provider = SMTPNotificationProvider({
            "from_address": "sender@example.com",
            "from_name": "Neut Bot",
            "smtp_use_tls": False,
        })
        provider.send(["user@example.com"], "Subject", "Body")

        msg_str = mock_server.sendmail.call_args[0][2]
        assert "Neut Bot" in msg_str


# ── Factory registration ─────────────────────────────────────────────

class TestFactoryRegistration:
    def test_smtp_registered(self):
        available = PublisherFactory.available("notification")
        assert "smtp" in available
