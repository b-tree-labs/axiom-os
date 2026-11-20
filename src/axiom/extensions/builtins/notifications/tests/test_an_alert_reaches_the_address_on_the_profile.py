# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""A recipient handle is not an address, and a failed channel is not hidden."""

from __future__ import annotations

import importlib
from types import SimpleNamespace

import pytest

from axiom.extensions.builtins.notifications import channel_config
from axiom.extensions.builtins.notifications.channels.base import ChannelAdapterRegistry
from axiom.extensions.builtins.notifications.channels.inbox import InboxChannelAdapterProvider
from axiom.extensions.builtins.notifications.channels.slack import SlackChannelAdapterProvider
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
from axiom.governance import Classification

send_module = importlib.import_module("axiom.extensions.builtins.notifications.send")


@pytest.fixture()
def profile():
    store = InMemoryRecipientPreferenceStore()
    store.put(
        RecipientProfile(
            recipient="@operator:senna",
            channels=(RecipientChannel("slack", "#senna-alerts"), RecipientChannel("inbox", "")),
        )
    )
    set_default_store(store)
    yield
    set_default_store(None)


def _send(monkeypatch, ok: bool):
    calls = []
    real = send_module._deliver_via

    def deliver(adapter, **kw):
        if isinstance(adapter, object) and adapter.__class__.__name__.lower().startswith("slack"):
            calls.append(kw["recipient"])
            return SimpleNamespace(ok=ok, error=None if ok else "refused by provider")
        return real(adapter, **kw)

    monkeypatch.setattr(send_module, "_deliver_via", deliver)
    store = InMemoryInboxStore()
    registry = ChannelAdapterRegistry()
    registry.register(InboxChannelAdapterProvider(store=store))
    registry.register(SlackChannelAdapterProvider())
    ctx = SendContext(registry=registry, inbox_store=store)
    ctx.channel_configs["slack"] = {"webhook_url": "https://hooks.example.org/x"}
    receipt = send(
        ctx,
        actor="@monitor:senna",
        recipient="@operator:senna",
        payload=NotificationPayload(summary="feed stopped"),
        classification=Classification.INTERNAL,
        priority=Priority.HIGH,
        intent="notification.send",
    )
    return calls, receipt


def test_a_channel_is_given_the_address_on_the_profile(profile, monkeypatch):
    calls, receipt = _send(monkeypatch, ok=True)
    assert calls == ["#senna-alerts"]
    assert receipt.channel_selected == "slack"


def test_a_failed_preferred_channel_stays_on_the_receipt(profile, monkeypatch):
    _, receipt = _send(monkeypatch, ok=False)
    assert receipt.channel_selected == "inbox"
    assert receipt.outcome == "succeeded"
    assert receipt.error == "refused by provider"


def test_an_unreadable_vault_reference_leaves_the_channel_off(monkeypatch, tmp_path):
    monkeypatch.setattr("axiom.infra.paths.get_user_state_dir", lambda: tmp_path)
    assert channel_config._from_vault("vault:not-there") == ""
    assert channel_config._from_vault("plain-value") == "plain-value"
