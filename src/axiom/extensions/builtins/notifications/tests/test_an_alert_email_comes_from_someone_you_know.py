# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""An alert email reads well and comes from a sender people recognise.

ADR-249 D4 (the message carries a readable HTML part and no metadata
footer), D7 (the sender registry is populated from HERALD config, so an
agent's own name and address apply in production) and acceptance A6 (a
provider's default address is a doctor finding, with the fix).

The failure this guards: an owner received ``[HIGH] scheduled dispatch
dead-lettered: ...`` from ``DoNotReply@<guid>.azurecomm.net`` with four
metadata lines appended to the body.
"""

from __future__ import annotations

import base64
import email
import json

import pytest

from axiom.extensions.builtins.notifications import channel_config
from axiom.extensions.builtins.notifications.channels.base import ChannelAdapterRegistry
from axiom.extensions.builtins.notifications.channels.email import (
    AcsEmailProvider,
    EmailChannelAdapter,
    EmailChannelAdapterProvider,
    EmailMessage,
    EmailSendResult,
)
from axiom.extensions.builtins.notifications.channels.inbox import (
    InboxChannelAdapterProvider,
)
from axiom.extensions.builtins.notifications.inbox import InMemoryInboxStore
from axiom.extensions.builtins.notifications.send import (
    ChannelPreferences,
    NotificationPayload,
    Priority,
    SendContext,
    resolve_sender,
    send,
)
from axiom.extensions.builtins.notifications.sender import (
    SenderIdentity,
    SenderRegistry,
    is_provider_default_sender,
    render_for_channel,
)
from axiom.governance import Classification

_HTML = "<p>Eight data refreshes failed.</p>"
_TEXT = "Eight data refreshes failed. Full record: https://node.example.org/app#/incidents/inc-1\n"


class _Recording:
    name = "stub"

    def __init__(self) -> None:
        self.calls: list[EmailMessage] = []

    def send(self, message: EmailMessage) -> EmailSendResult:
        self.calls.append(message)
        return EmailSendResult(ok=True, provider="stub", message_id="m-1")


class _StubEmailProvider(EmailChannelAdapterProvider):
    def __init__(self, backend: _Recording) -> None:
        self._backend = backend

    def build(self, config=None):
        cfg = config or {}
        return EmailChannelAdapter(
            provider=self._backend,
            from_address=cfg["from_address"],
            from_name=cfg.get("from_name", ""),
        )


def _ctx(backend: _Recording, *, registry: SenderRegistry | None = None) -> SendContext:
    store = InMemoryInboxStore()
    reg = ChannelAdapterRegistry()
    reg.register(InboxChannelAdapterProvider(store=store))
    reg.register(_StubEmailProvider(backend))
    ctx = SendContext(registry=reg, inbox_store=store, sender_registry=registry)
    ctx.channel_configs["email"] = {"from_address": "alerts@node.example.org"}
    return ctx


def _send(ctx, **kw):
    return send(
        ctx,
        actor=kw.pop("actor", "@plinth"),
        recipient="owner@example.org",
        payload=kw.pop("payload", NotificationPayload(summary="8 data refreshes failing", body=_TEXT)),
        classification=Classification.INTERNAL,
        priority=kw.pop("priority", Priority.HIGH),
        channel_prefs=ChannelPreferences(ordered_channels=("email", "inbox")),
        **kw,
    )


# ------------------------------------------------------------ send + email --


def test_send_passes_body_html_through_to_the_email_channel():
    backend = _Recording()
    receipt = _send(_ctx(backend), body_html=_HTML)
    assert receipt.outcome == "succeeded" and receipt.channel_selected == "email"
    msg = backend.calls[0]
    assert msg.body_html == _HTML
    assert msg.body_text == _TEXT


def test_send_without_body_html_is_unchanged():
    backend = _Recording()
    _send(_ctx(backend))
    msg = backend.calls[0]
    assert msg.body_html is None
    assert msg.subject == "[HIGH] 8 data refreshes failing"


def test_an_alert_desk_message_has_no_priority_prefix():
    backend = _Recording()
    _send(_ctx(backend), body_html=_HTML)
    assert backend.calls[0].subject == "8 data refreshes failing"


def test_channels_that_cannot_take_html_still_deliver():
    store = InMemoryInboxStore()
    reg = ChannelAdapterRegistry()
    reg.register(InboxChannelAdapterProvider(store=store))
    ctx = SendContext(registry=reg, inbox_store=store)
    receipt = _send(ctx, body_html=_HTML)
    assert receipt.outcome == "succeeded" and receipt.channel_selected == "inbox"


def test_no_metadata_footer_when_a_body_is_given():
    backend = _Recording()
    _send(_ctx(backend), body_html=_HTML)
    text = backend.calls[0].body_text
    for line in ("to:", "priority:", "classification:", "receipt:"):
        assert line not in text
    headers = backend.calls[0].headers
    assert headers["X-Axiom-Priority"] == "high"
    assert headers["X-Axiom-Receipt-Id"].startswith("rcpt-")


def test_existing_callers_keep_the_metadata_footer_and_prefix():
    backend = _Recording()
    _send(_ctx(backend), payload=NotificationPayload(summary="disk full"))
    msg = backend.calls[0]
    assert msg.subject == "[HIGH] disk full"
    assert "receipt:" in msg.body_text


def test_smtp_sends_multipart_alternative():
    from axiom.extensions.builtins.notifications.channels.email.smtp import _build_mime

    msg = EmailMessage(
        to=("a@example.org",), subject="s", from_address="f@example.org",
        body_text=_TEXT, body_html=_HTML,
    )
    mime = _build_mime(msg)
    assert mime.get_content_subtype() == "alternative"
    kinds = [p.get_content_type() for p in mime.get_payload()]
    assert kinds == ["text/plain", "text/html"]


def test_gmail_sends_multipart_alternative_when_both_bodies_exist():
    from axiom.extensions.builtins.notifications.channels.email.gmail import _build_raw

    msg = EmailMessage(
        to=("a@example.org",), subject="s", from_address="f@example.org",
        body_text=_TEXT, body_html=_HTML,
    )
    parsed = email.message_from_bytes(base64.urlsafe_b64decode(_build_raw(msg)))
    assert parsed.get_content_type() == "multipart/alternative"
    assert [p.get_content_type() for p in parsed.get_payload()] == ["text/plain", "text/html"]


class _Poster:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def post(self, url, *, content, headers, timeout):
        self.calls.append(json.loads(content))

        class _R:
            status_code = 202
            headers: dict = {}
            text = ""

            def json(self):
                return {"id": "op-1"}

        return _R()


def test_acs_payload_carries_plain_text_and_html():
    poster = _Poster()
    p = AcsEmailProvider(endpoint="https://res.example.org", access_token="tok", poster=poster)
    r = p.send(EmailMessage(
        to=("a@example.org",), subject="s", from_address="alerts@node.example.org",
        body_text=_TEXT, body_html=_HTML,
    ))
    assert r.ok
    content = poster.calls[0]["content"]
    assert content["plainText"] == _TEXT
    assert content["html"] == _HTML


# ------------------------------------------------------------------ sender --


def _hermetic(monkeypatch, *, herald: dict | None = None, sender: dict | None = None):
    monkeypatch.setattr(channel_config, "_load_installed", lambda: dict(herald or {}))
    monkeypatch.setattr(channel_config, "_load_sender_tables", lambda: dict(sender or {}))
    for var in (
        "AXIOM_HERALD_SENDER_FROM_ADDRESS",
        "AXIOM_HERALD_SENDER_FROM_NAME",
        "AXIOM_HERALD_EMAIL_FROM_ADDRESS",
        "AXIOM_HERALD_EMAIL_FROM_NAME",
    ):
        monkeypatch.delenv(var, raising=False)


def test_default_context_populates_the_sender_registry_from_config(monkeypatch):
    _hermetic(
        monkeypatch,
        sender={
            "from_address": "agents@node.example.org",
            "plinth": {"from_address": "plinth@node.example.org"},
            "rivet": {"display_name": "Rivet", "from_name": "Release desk"},
        },
    )
    ctx = SendContext.default()
    assert ctx.sender_registry is not None
    plinth = resolve_sender("@plinth", ctx)
    assert plinth.from_address == "plinth@node.example.org"
    assert render_for_channel(plinth, "email").display == "PLINTH via Axiom"
    rivet = resolve_sender("@rivet:local", ctx)
    assert rivet.from_address == "agents@node.example.org"
    assert render_for_channel(rivet, "email").display == "Release desk"


def test_an_unconfigured_agent_still_gets_its_name_via_the_platform(monkeypatch):
    _hermetic(monkeypatch)
    ident = resolve_sender("@plinth:node", SendContext.default())
    assert render_for_channel(ident, "email").display == "PLINTH via Axiom"
    assert ident.from_address in (None, "")


def test_the_site_wide_sender_address_applies_to_every_agent(monkeypatch):
    _hermetic(monkeypatch)
    monkeypatch.setenv("AXIOM_HERALD_SENDER_FROM_ADDRESS", "alerts@site.example.org")
    ident = resolve_sender("@tidy", SendContext.default())
    assert ident.from_address == "alerts@site.example.org"


def test_the_email_from_line_uses_the_agent_name_and_address():
    backend = _Recording()
    registry = SenderRegistry()
    registry.register(
        SenderIdentity(
            principal="@plinth",
            display_name="PLINTH",
            version="",
            owner_handle="",
            from_address="plinth@node.example.org",
            email_display="PLINTH via Axiom",
        )
    )
    _send(_ctx(backend, registry=registry), body_html=_HTML)
    msg = backend.calls[0]
    assert msg.from_name == "PLINTH via Axiom"
    assert msg.from_address == "plinth@node.example.org"


def test_possessive_surfaces_are_unchanged_for_other_channels():
    ident = SenderIdentity(
        principal="@rivet:bens", display_name="RIVET", version="0.6.0",
        owner_handle="Ben", email_display="RIVET via Axiom",
    )
    assert render_for_channel(ident, "slack").display == "Ben's RIVET 0.6.0"


# ------------------------------------------------------------------ doctor --


@pytest.mark.parametrize(
    "addr,expected",
    [
        ("DoNotReply@0f3c9a.azurecomm.net", True),
        ("donotreply@abc.europe.azurecomm.net", True),
        ("alerts@node.example.org", False),
        ("", False),
    ],
)
def test_provider_default_sender_is_recognised(addr, expected):
    assert is_provider_default_sender(addr) is expected


def test_doctor_flags_the_provider_default_sender(monkeypatch):
    from axiom.cli.doctor import CheckStatus, check_herald_sender

    _hermetic(monkeypatch)
    monkeypatch.setenv("AXIOM_HERALD_EMAIL_FROM_ADDRESS", "DoNotReply@0f3c9a.azurecomm.net")
    result = check_herald_sender()
    assert result.status == CheckStatus.WARNING
    assert "provider" in result.summary
    assert "verify a sender domain" in result.fix_hint
    assert "email_from_address" in result.fix_hint


def test_doctor_flags_a_provider_default_on_one_agent(monkeypatch):
    from axiom.cli.doctor import CheckStatus, check_herald_sender

    _hermetic(monkeypatch, sender={"plinth": {"from_address": "DoNotReply@x.azurecomm.net"}})
    monkeypatch.setenv("AXIOM_HERALD_EMAIL_FROM_ADDRESS", "alerts@node.example.org")
    result = check_herald_sender()
    assert result.status == CheckStatus.WARNING
    assert "plinth" in result.summary


def test_doctor_passes_a_verified_sender(monkeypatch):
    from axiom.cli.doctor import CheckStatus, check_herald_sender

    _hermetic(monkeypatch)
    monkeypatch.setenv("AXIOM_HERALD_EMAIL_FROM_ADDRESS", "alerts@node.example.org")
    assert check_herald_sender().status == CheckStatus.OK


def test_doctor_skips_when_no_email_is_configured(monkeypatch):
    from axiom.cli.doctor import CheckStatus, check_herald_sender

    _hermetic(monkeypatch)
    assert check_herald_sender().status == CheckStatus.SKIPPED


def test_a_verified_site_sender_clears_a_provider_default_channel_address(monkeypatch):
    from axiom.cli.doctor import CheckStatus, check_herald_sender

    _hermetic(monkeypatch, sender={"from_address": "alerts@site.example.org"})
    monkeypatch.setenv("AXIOM_HERALD_EMAIL_FROM_ADDRESS", "DoNotReply@0f3c9a.azurecomm.net")
    assert check_herald_sender().status == CheckStatus.OK
