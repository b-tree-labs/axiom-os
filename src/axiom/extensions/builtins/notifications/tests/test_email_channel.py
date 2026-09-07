# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tests for the HERALD ``email`` channel — nested Factory/Provider over
cloud email backends.

Pins:
- ``EmailMessage`` value-object validation
- ``EmailProvider`` registry + ``detect_email_provider`` resolution
- ``SmtpEmailProvider`` send (with injected smtp_factory)
- ``ResendEmailProvider`` send (with injected poster)
- ``EmailChannelAdapter`` wraps the chosen provider; ceiling INTERNAL;
  outbound only; required-config errors
- Secret-redaction round-trip (Resend ``re_xxx`` key never round-trips
  through error text)
"""

from __future__ import annotations

import pytest

from axiom.extensions.builtins.notifications.channels.base import Direction
from axiom.extensions.builtins.notifications.channels.email import (
    EmailChannelAdapter,
    EmailChannelAdapterProvider,
    EmailMessage,
    EmailSendResult,
    ResendEmailProvider,
    SmtpConfig,
    SmtpEmailProvider,
    detect_email_provider,
    email_provider_names,
    register_email_provider,
)
from axiom.governance import Classification

# ---------------------------------------------------------------------------
# EmailMessage value object
# ---------------------------------------------------------------------------


class TestEmailMessage:
    def test_requires_recipient(self):
        with pytest.raises(ValueError, match="to"):
            EmailMessage(
                to=(), subject="x", from_address="a@b", body_text="x"
            )

    def test_requires_some_body(self):
        with pytest.raises(ValueError, match="body"):
            EmailMessage(to=("a@b",), subject="x", from_address="a@b")

    def test_text_only_ok(self):
        m = EmailMessage(
            to=("a@b",), subject="x", from_address="a@b", body_text="hi"
        )
        assert m.body_text == "hi"
        assert m.body_html is None

    def test_html_only_ok(self):
        m = EmailMessage(
            to=("a@b",), subject="x", from_address="a@b", body_html="<p>hi</p>"
        )
        assert m.body_html == "<p>hi</p>"

    def test_immutable_recipient_tuple(self):
        m = EmailMessage(
            to=("a@b", "c@d"), subject="x", from_address="a@b", body_text="x"
        )
        # tuple, not list — frozen value object.
        assert isinstance(m.to, tuple)
        # Frozen — attribute assignment fails.
        with pytest.raises(Exception):
            m.to = ("x@y",)  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Factory / registry
# ---------------------------------------------------------------------------


class TestFactory:
    def test_smtp_and_resend_registered_at_import(self):
        names = email_provider_names()
        assert "smtp" in names
        assert "resend" in names

    def test_detect_returns_none_when_no_keys(self):
        assert detect_email_provider({}) is None

    def test_detect_resend_by_api_key(self):
        p = detect_email_provider({"resend_api_key": "re_test"})
        assert isinstance(p, ResendEmailProvider)
        assert p.name == "resend"

    def test_detect_smtp_by_host(self):
        p = detect_email_provider({"smtp_host": "localhost"})
        assert isinstance(p, SmtpEmailProvider)
        assert p.name == "smtp"

    def test_vendor_key_wins_over_smtp_fallback(self):
        # When both are set, the more-specific vendor-API match wins.
        p = detect_email_provider(
            {"resend_api_key": "re_x", "smtp_host": "localhost"}
        )
        assert p.name == "resend"

    def test_explicit_provider_override(self):
        p = detect_email_provider(
            {"provider": "smtp", "smtp_host": "localhost"}
        )
        assert p.name == "smtp"

    def test_register_new_provider_via_factory(self):
        class _Stub:
            name = "stub"

            def send(self, message):
                return EmailSendResult(ok=True, provider="stub")

        register_email_provider("stub", lambda cfg: _Stub(), replace=True)
        assert "stub" in email_provider_names()
        p = detect_email_provider({"provider": "stub"})
        assert p.name == "stub"

    def test_duplicate_register_raises_without_replace(self):
        with pytest.raises(ValueError, match="already registered"):
            register_email_provider("smtp", lambda cfg: None)


# ---------------------------------------------------------------------------
# Resend backend
# ---------------------------------------------------------------------------


class _FakePoster:
    def __init__(self, *, status_code=200, body=None, raise_exc=None):
        self.status_code = status_code
        self.body = body if body is not None else {"id": "msg-abc-123"}
        self.raise_exc = raise_exc
        self.calls = []

    def post(self, url, json, headers, timeout):
        self.calls.append(
            {"url": url, "json": json, "headers": headers, "timeout": timeout}
        )
        if self.raise_exc is not None:
            raise self.raise_exc
        return _FakeResp(self.status_code, self.body)


class _FakeResp:
    def __init__(self, status_code, body):
        self.status_code = status_code
        self._body = body
        self.text = str(body)

    def json(self):
        return self._body


def _msg(**overrides):
    base = dict(
        to=("alice@example.com",),
        subject="DP1 ingest complete",
        from_address="herald@b-tree.labs",
        body_text="ingest finished",
    )
    base.update(overrides)
    return EmailMessage(**base)


class TestResendProvider:
    def test_happy_path_posts_and_returns_message_id(self):
        poster = _FakePoster()
        p = ResendEmailProvider(api_key="re_test", poster=poster)
        result = p.send(_msg())
        assert result.ok is True
        assert result.provider == "resend"
        assert result.message_id == "msg-abc-123"
        call = poster.calls[0]
        assert call["url"] == "https://api.resend.com/emails"
        assert call["headers"]["Authorization"] == "Bearer re_test"
        assert call["json"]["to"] == ["alice@example.com"]
        assert call["json"]["text"] == "ingest finished"

    def test_html_body_routed(self):
        poster = _FakePoster()
        p = ResendEmailProvider(api_key="re_test", poster=poster)
        p.send(_msg(body_text=None, body_html="<b>done</b>"))
        assert poster.calls[0]["json"]["html"] == "<b>done</b>"
        assert "text" not in poster.calls[0]["json"]

    def test_from_name_renders_in_envelope(self):
        poster = _FakePoster()
        p = ResendEmailProvider(api_key="re_test", poster=poster)
        p.send(_msg(from_name="HERALD"))
        assert (
            poster.calls[0]["json"]["from"]
            == "HERALD <herald@b-tree.labs>"
        )

    def test_4xx_returns_failure(self):
        poster = _FakePoster(status_code=422, body={"error": "invalid"})
        p = ResendEmailProvider(api_key="re_test", poster=poster)
        result = p.send(_msg())
        assert result.ok is False
        assert "422" in result.error

    def test_network_exception_returns_failure(self):
        poster = _FakePoster(raise_exc=ConnectionError("dns"))
        p = ResendEmailProvider(api_key="re_test", poster=poster)
        result = p.send(_msg())
        assert result.ok is False
        assert "dns" in result.error or "ConnectionError" in result.error

    def test_api_key_redacted_from_error_text(self):
        # Resend sometimes echoes the masked key in error bodies; the
        # provider strips any ``re_xxx`` token from the returned error.
        poster = _FakePoster(
            status_code=500,
            body={"error": "see key re_supersecretkey123"},
        )
        p = ResendEmailProvider(api_key="re_supersecretkey123", poster=poster)
        result = p.send(_msg())
        assert "supersecretkey123" not in (result.error or "")


# ---------------------------------------------------------------------------
# SMTP backend (with injected smtplib fake)
# ---------------------------------------------------------------------------


class _FakeSmtp:
    def __init__(self, host, port, timeout):
        self.host = host
        self.port = port
        self.timeout = timeout
        self.tls_called = False
        self.login_called: tuple[str, str] | None = None
        self.sent: list[tuple[str, list[str], str]] = []
        self.quit_called = False

    def starttls(self):
        self.tls_called = True

    def login(self, user, password):
        self.login_called = (user, password)

    def sendmail(self, from_addr, to_addrs, body):
        self.sent.append((from_addr, to_addrs, body))

    def quit(self):
        self.quit_called = True


class TestSmtpProvider:
    def _provider(self, **cfg_overrides):
        cfg = SmtpConfig(host="mx.test", port=587, username="u", password="p")
        for k, v in cfg_overrides.items():
            cfg = SmtpConfig(
                **{**cfg.__dict__, k: v}
            )
        fakes: list[_FakeSmtp] = []

        def factory(host, port, timeout=30.0):
            f = _FakeSmtp(host, port, timeout)
            fakes.append(f)
            return f

        return SmtpEmailProvider(cfg, smtp_factory=factory), fakes

    def test_happy_path_sends_and_quits(self):
        p, fakes = self._provider()
        result = p.send(_msg())
        assert result.ok is True
        assert result.provider == "smtp"
        fake = fakes[0]
        assert fake.tls_called is True
        assert fake.login_called == ("u", "p")
        assert len(fake.sent) == 1
        from_addr, to_addrs, body = fake.sent[0]
        assert from_addr == "herald@b-tree.labs"
        assert to_addrs == ["alice@example.com"]
        assert "DP1 ingest complete" in body
        assert fake.quit_called is True

    def test_no_auth_when_user_blank(self):
        p, fakes = self._provider(username="", password="")
        p.send(_msg())
        assert fakes[0].login_called is None

    def test_no_tls_when_disabled(self):
        p, fakes = self._provider(use_tls=False)
        p.send(_msg())
        assert fakes[0].tls_called is False

    def test_cc_and_bcc_included_in_envelope(self):
        p, fakes = self._provider()
        p.send(_msg(cc=("watch@example.com",), bcc=("audit@example.com",)))
        _, to_addrs, body = fakes[0].sent[0]
        assert to_addrs == [
            "alice@example.com", "watch@example.com", "audit@example.com",
        ]
        # Bcc must NOT appear in the rendered headers (per RFC).
        assert "audit@example.com" not in body.split("\n\n")[0]

    def test_connection_failure_returns_structured_error(self):
        def factory(host, port, timeout=30.0):
            raise ConnectionRefusedError("smtp down")

        cfg = SmtpConfig(host="mx.test")
        p = SmtpEmailProvider(cfg, smtp_factory=factory)
        result = p.send(_msg())
        assert result.ok is False
        assert "smtp down" in result.error or "ConnectionRefusedError" in result.error


# ---------------------------------------------------------------------------
# EmailChannelAdapter — the HERALD outer wrapper
# ---------------------------------------------------------------------------


class _RecordingProvider:
    name = "stub"

    def __init__(self):
        self.calls: list[EmailMessage] = []

    def send(self, message):
        self.calls.append(message)
        return EmailSendResult(ok=True, provider="stub", message_id="msg-1")


class TestEmailChannelAdapter:
    def test_requires_from_address(self):
        with pytest.raises(ValueError, match="from_address"):
            EmailChannelAdapter(
                provider=_RecordingProvider(), from_address=""
            )

    def test_deliver_sync_routes_through_provider(self):
        provider = _RecordingProvider()
        adapter = EmailChannelAdapter(
            provider=provider, from_address="herald@b-tree.labs"
        )
        result = adapter.deliver_sync(
            recipient="alice@example.com",
            receipt_id="r-1",
            classification=Classification.INTERNAL,
            priority="normal",
            summary="hello",
        )
        assert result.ok is True
        assert result.provider == "stub"
        assert result.message_id == "msg-1"
        # Underlying provider saw the rendered EmailMessage.
        msg = provider.calls[0]
        assert msg.to == ("alice@example.com",)
        assert msg.subject == "hello"
        assert msg.from_address == "herald@b-tree.labs"
        # Provenance headers stamped.
        assert msg.headers["X-Axiom-Receipt-Id"] == "r-1"
        assert msg.headers["X-Axiom-Classification"] == "internal"
        assert msg.headers["X-Axiom-Priority"] == "normal"

    def test_urgent_prefix_in_subject(self):
        provider = _RecordingProvider()
        adapter = EmailChannelAdapter(
            provider=provider, from_address="herald@b-tree.labs"
        )
        adapter.deliver_sync(
            recipient="x@y",
            receipt_id="r",
            classification=Classification.INTERNAL,
            priority="urgent",
            summary="trunk red",
        )
        assert provider.calls[0].subject == "[URGENT] trunk red"

    def test_capabilities_outbound_internal_ceiling(self):
        caps = EmailChannelAdapterProvider().capabilities()
        assert caps.direction == Direction.OUTBOUND
        assert caps.classification_ceiling == Classification.INTERNAL
        assert caps.supports_threading is True
        assert caps.supports_acknowledge is False

    def test_provider_build_requires_from_address(self):
        with pytest.raises(ValueError, match="from_address"):
            EmailChannelAdapterProvider().build(
                {"smtp_host": "localhost"}
            )

    def test_provider_build_requires_backend(self):
        with pytest.raises(ValueError, match="backend config"):
            EmailChannelAdapterProvider().build(
                {"from_address": "herald@b-tree.labs"}
            )

    def test_provider_build_picks_resend_when_keyed(self):
        adapter = EmailChannelAdapterProvider().build(
            {
                "from_address": "herald@b-tree.labs",
                "resend_api_key": "re_test",
            }
        )
        assert adapter.name == "email"

    def test_provider_build_picks_smtp_when_keyed(self):
        adapter = EmailChannelAdapterProvider().build(
            {
                "from_address": "herald@b-tree.labs",
                "smtp_host": "localhost",
            }
        )
        assert adapter.name == "email"
