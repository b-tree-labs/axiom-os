# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""An alert that reaches no person is not delivered (#1597).

On one node, hundreds of failure alerts to ``@operator`` were recorded
``outcome=succeeded`` through the inbox. Nobody held the operator role there and
no recipient profile existed, so nobody could read any of them, and a nightly
failure ran unseen for weeks.

A role address that nobody holds on the node and that has no recipient profile
reaches no person. Its inbox row is still kept, but the receipt says
``undelivered`` and why. A person's own handle is not affected: a person reads
their inbox whether or not they set up a profile.
"""

from __future__ import annotations

import pytest

from axiom.extensions.builtins.notifications.channels.inbox import InboxChannelAdapterProvider
from axiom.extensions.builtins.notifications.inbox import InMemoryInboxStore
from axiom.extensions.builtins.notifications.preferences import (
    InMemoryRecipientPreferenceStore,
    RecipientChannel,
    RecipientProfile,
    set_default_store,
)
from axiom.extensions.builtins.notifications.send import (
    NotificationPayload,
    Priority,
    SendContext,
    send,
)
from axiom.extensions.builtins.settings import store as settings_store
from axiom.governance import Classification


class _Settings:
    def __init__(self, table):
        self.table = table

    def get(self, key, default=None):
        return self.table.get(key, default)


class _RecordingInbox(InMemoryInboxStore):
    """An inbox that records what the durable receipt row was closed with."""

    def __init__(self):
        super().__init__()
        self.closed = {}

    def close_receipt(self, receipt_id, **fields):
        self.closed[receipt_id] = fields


@pytest.fixture()
def profiles(monkeypatch):
    store = InMemoryRecipientPreferenceStore()
    set_default_store(store)
    yield store
    set_default_store(None)


def _holder(monkeypatch, holder=""):
    monkeypatch.setattr(settings_store, "SettingsStore",
                        lambda: _Settings({"notifications.roles.operator": holder}))


def _send(recipient, inbox=None):
    inbox = inbox if inbox is not None else _RecordingInbox()
    ctx = SendContext(inbox_store=inbox)
    ctx.registry.register(InboxChannelAdapterProvider(store=inbox))
    return send(ctx, actor="@pulse", recipient=recipient,
                payload=NotificationPayload(summary="the nightly restore failed"),
                classification=Classification.INTERNAL, priority=Priority.HIGH)


def test_an_unheld_role_with_no_profile_is_undelivered(profiles, monkeypatch):
    _holder(monkeypatch)
    inbox = _RecordingInbox()
    receipt = _send("@operator", inbox)
    assert receipt.outcome == "undelivered"
    assert receipt.channel_selected == "inbox"
    assert "notifications.roles.operator" in (receipt.error or "")
    assert inbox.closed[receipt.id]["outcome"] == "undelivered"


def test_a_role_held_by_a_person_is_delivered(profiles, monkeypatch):
    _holder(monkeypatch, "@person")
    receipt = _send("@operator")
    assert receipt.recipient == "@person"
    assert receipt.outcome == "succeeded"


def test_a_role_with_its_own_profile_is_delivered(profiles, monkeypatch):
    _holder(monkeypatch)
    profiles.put(RecipientProfile(recipient="@operator",
                                  channels=(RecipientChannel("inbox", "@operator"),)))
    assert _send("@operator").outcome == "succeeded"


def test_a_persons_handle_without_a_profile_is_unchanged(profiles, monkeypatch):
    _holder(monkeypatch)
    assert _send("@someone:example-site").outcome == "succeeded"
