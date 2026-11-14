# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0

"""Adapters apply the servant nameplate when a sender is supplied (ADR-066 PR-2).

These pin the visible surface: Slack/Mattermost ``username`` + ``icon_url``,
SMS body prefix, and the email From — plus the render verbatim-owner rule
and send()'s ``resolve_sender`` wiring.
"""

from __future__ import annotations

from axiom.extensions.builtins.notifications.channels.mattermost import (
    MattermostChannelAdapter,
)
from axiom.extensions.builtins.notifications.channels.slack import (
    SlackChannelAdapter,
)
from axiom.extensions.builtins.notifications.channels.twilio_sms import (
    TwilioSmsChannelAdapter,
)
from axiom.extensions.builtins.notifications.sender import (
    SenderIdentity,
    render_for_channel,
)
from axiom.governance import Classification


def _sender() -> SenderIdentity:
    # owner_handle holds a *resolved* display ("Ben") → used verbatim.
    return SenderIdentity(
        principal="@rivet:bens",
        display_name="RIVET",
        version="0.6.0",
        owner_handle="Ben",
        avatar_uri="https://axiom.btreelabs.ai/avatars/rivet.png",
    )


class _JsonPoster:
    def __init__(self):
        self.calls = []

    def post(self, url, json, timeout):
        self.calls.append({"url": url, "json": json, "timeout": timeout})
        return _Resp()


class _DataPoster:
    def __init__(self):
        self.calls = []

    def post(self, url, data, auth, headers, timeout):
        self.calls.append({"data": data})
        return _Resp(201)


class _Resp:
    def __init__(self, status_code=200):
        self.status_code = status_code
        self.text = '{"sid": "SM1"}'

    def json(self):
        return {"sid": "SM1"}


# --- render verbatim-owner rule ------------------------------------------- #
def test_render_uses_resolved_owner_verbatim():
    # plain owner string (resolved) is used as-is, not munged
    r = render_for_channel(_sender(), "slack")
    assert r.display == "Ben's RIVET 0.6.0"


def test_render_host_owner_not_mangled():
    ident = SenderIdentity(
        principal="@rivet:bens", display_name="RIVET", version="0.6.0",
        owner_handle="ben-mbp",
    )
    r = render_for_channel(ident, "slack")
    assert r.display == "ben-mbp's RIVET 0.6.0"


def test_render_at_handle_still_munged_as_fallback():
    ident = SenderIdentity(
        principal="@rivet:bens", display_name="RIVET", version="0.6.0",
        owner_handle="@ben.booth",
    )
    r = render_for_channel(ident, "slack")
    assert r.display == "Ben's RIVET 0.6.0"


# --- per-channel application ----------------------------------------------- #
def test_slack_applies_username_and_icon():
    poster = _JsonPoster()
    SlackChannelAdapter(webhook_url="https://hooks/x", poster=poster).deliver_sync(
        recipient="#general", receipt_id="r1",
        classification=Classification.INTERNAL, priority="normal",
        summary="ingest done", sender=_sender(),
    )
    payload = poster.calls[0]["json"]
    assert payload["username"] == "Ben's RIVET 0.6.0"
    assert payload["icon_url"].endswith("rivet.png")


def test_slack_no_sender_omits_username():
    poster = _JsonPoster()
    SlackChannelAdapter(webhook_url="https://hooks/x", poster=poster).deliver_sync(
        recipient="#general", receipt_id="r1",
        classification=Classification.INTERNAL, priority="normal",
        summary="ingest done",
    )
    assert "username" not in poster.calls[0]["json"]


def test_mattermost_applies_username():
    poster = _JsonPoster()
    MattermostChannelAdapter(webhook_url="https://mm/x", poster=poster).deliver_sync(
        recipient="#general", receipt_id="r1",
        classification=Classification.INTERNAL, priority="normal",
        summary="ingest done", sender=_sender(),
    )
    assert poster.calls[0]["json"]["username"] == "Ben's RIVET 0.6.0"


def test_sms_prefixes_body():
    poster = _DataPoster()
    TwilioSmsChannelAdapter(
        account_sid="AC", auth_token="tok", from_number="+15125550100",
        poster=poster,
    ).deliver_sync(
        recipient="+15125550199", receipt_id="r1",
        classification=Classification.INTERNAL, priority="normal",
        summary="ingest done", sender=_sender(),
    )
    assert poster.calls[0]["data"]["Body"].startswith("[Ben's RIVET]")


# --- send.resolve_sender --------------------------------------------------- #
def test_resolve_sender_derives_caps_display_and_owner():
    from axiom.extensions.builtins.notifications.send import SendContext, resolve_sender

    ident = resolve_sender("@rivet:bens", SendContext.default())
    assert ident.display_name == "RIVET"
    assert ident.principal == "@rivet:bens"
    assert ident.owner_handle  # never empty (user.name | peer | birth-host)
