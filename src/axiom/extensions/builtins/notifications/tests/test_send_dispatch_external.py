# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0

"""send() dispatch to EXTERNAL channels + the fail-closed ceiling invariant.

Closes the SEC-1 gap where only the inbox channel could succeed. Uses the
Slack adapter (with an injected HTTP poster) as a representative external
(INTERNAL-ceiling) channel.

The compliance-load-bearing test: a ``regulated`` / ``controlled``
(EC-controlled / ITAR) envelope must NEVER be admitted to an external
channel and must fall back to the inbox.
"""

from __future__ import annotations

import pytest

from axiom.extensions.builtins.notifications.channel_config import (
    rehydrate_from_env,
)
from axiom.extensions.builtins.notifications.channels.base import (
    ChannelAdapterRegistry,
)
from axiom.extensions.builtins.notifications.channels.inbox import (
    InboxChannelAdapterProvider,
)
from axiom.extensions.builtins.notifications.channels.slack import (
    SlackChannelAdapterProvider,
)
from axiom.extensions.builtins.notifications.inbox import InMemoryInboxStore
from axiom.extensions.builtins.notifications.send import (
    ChannelPreferences,
    NotificationPayload,
    Priority,
    SendContext,
    send,
)
from axiom.governance import Classification

WEBHOOK = "https://hooks.slack.com/services/T000/B000/xxxxxxxxxxxx"


class _FakeResp:
    def __init__(self, status_code=200, text="ok"):
        self.status_code = status_code
        self.text = text


class _CountingPoster:
    def __init__(self, status_code=200):
        self.status_code = status_code
        self.calls = 0

    def post(self, *args, **kwargs):
        self.calls += 1
        return _FakeResp(self.status_code)


def _ctx_with_slack(*, poster=None, configured=True):
    """A SendContext with inbox + slack registered.

    When ``configured`` is True the slack channel gets a dispatch config
    (webhook + injected poster); otherwise it is registered-but-unconfigured
    to exercise the fail-closed path.
    """
    store = InMemoryInboxStore()
    registry = ChannelAdapterRegistry()
    registry.register(InboxChannelAdapterProvider(store=store))
    registry.register(SlackChannelAdapterProvider())
    ctx = SendContext(registry=registry, inbox_store=store)
    if configured:
        ctx.channel_configs["slack"] = {
            "webhook_url": WEBHOOK,
            "poster": poster or _CountingPoster(),
        }
    return ctx, store


def test_external_channel_dispatches_to_succeeded():
    """The gap-closer: a non-inbox channel now dispatches to 'succeeded'."""
    poster = _CountingPoster(status_code=200)
    ctx, store = _ctx_with_slack(poster=poster)
    receipt = send(
        ctx,
        actor="@agent:test",
        recipient="@op:test",
        payload=NotificationPayload(summary="pump trip"),
        classification=Classification.INTERNAL,
        priority=Priority.HIGH,
        channel_prefs=ChannelPreferences(ordered_channels=("slack", "inbox")),
    )
    assert receipt.outcome == "succeeded"
    assert receipt.channel_selected == "slack"
    assert poster.calls == 1
    # It did NOT fall through to the inbox.
    assert store.all() == []


@pytest.mark.parametrize(
    "classification",
    [Classification.REGULATED, Classification.CONTROLLED],
)
def test_controlled_never_admitted_to_external(classification):
    """EC-controlled / ITAR envelopes fail closed to the inbox.

    The external (INTERNAL-ceiling) slack channel must not be admitted;
    the send lands in the inbox instead, and the poster is never called.
    """
    poster = _CountingPoster(status_code=200)
    ctx, store = _ctx_with_slack(poster=poster)
    receipt = send(
        ctx,
        actor="@agent:test",
        recipient="@rso:test",
        payload=NotificationPayload(summary="controlled alert"),
        classification=classification,
        priority=Priority.URGENT,
        # Even when the caller *asks* for slack, the ceiling wins.
        channel_prefs=ChannelPreferences(ordered_channels=("slack", "inbox")),
    )
    assert receipt.outcome == "succeeded"
    assert receipt.channel_selected == "inbox"
    assert poster.calls == 0
    assert len(store.all()) == 1
    # The rationale records slack as inspected-but-not-admitted.
    slack_row = next(
        r for r in receipt.routing_rationale if r["adapter"] == "slack"
    )
    assert slack_row["admitted"] is False


def test_unconfigured_external_fails_closed_to_inbox():
    """A registered-but-unconfigured external channel is skipped → inbox."""
    ctx, store = _ctx_with_slack(configured=False)
    receipt = send(
        ctx,
        actor="@agent:test",
        recipient="@op:test",
        payload=NotificationPayload(summary="x"),
        classification=Classification.INTERNAL,
        priority=Priority.HIGH,
        channel_prefs=ChannelPreferences(ordered_channels=("slack", "inbox")),
    )
    assert receipt.outcome == "succeeded"
    assert receipt.channel_selected == "inbox"
    assert len(store.all()) == 1


def test_external_delivery_failure_falls_back_to_inbox():
    """When the external channel errors, the alert still lands (inbox)."""
    poster = _CountingPoster(status_code=500)
    ctx, store = _ctx_with_slack(poster=poster)
    receipt = send(
        ctx,
        actor="@agent:test",
        recipient="@op:test",
        payload=NotificationPayload(summary="x"),
        classification=Classification.INTERNAL,
        priority=Priority.HIGH,
        channel_prefs=ChannelPreferences(ordered_channels=("slack", "inbox")),
    )
    assert poster.calls == 1
    assert receipt.outcome == "succeeded"
    assert receipt.channel_selected == "inbox"
    assert len(store.all()) == 1


# ---------------------------------------------------------------------------
# Rehydration from environment
# ---------------------------------------------------------------------------


def test_rehydrate_enables_fully_configured_channel():
    store = InMemoryInboxStore()
    registry = ChannelAdapterRegistry()
    registry.register(InboxChannelAdapterProvider(store=store))
    ctx = SendContext(registry=registry, inbox_store=store)

    enabled = rehydrate_from_env(
        ctx, env={"AXIOM_HERALD_SLACK_WEBHOOK_URL": WEBHOOK}
    )
    assert "slack" in enabled
    assert "slack" in ctx.registry.names()
    assert ctx.channel_configs["slack"]["webhook_url"] == WEBHOOK


def test_rehydrate_skips_partial_config_fail_closed():
    store = InMemoryInboxStore()
    registry = ChannelAdapterRegistry()
    registry.register(InboxChannelAdapterProvider(store=store))
    ctx = SendContext(registry=registry, inbox_store=store)

    # Twilio needs SID + token + from-number; supplying only the SID must
    # NOT enable the channel.
    enabled = rehydrate_from_env(
        ctx, env={"AXIOM_HERALD_TWILIO_ACCOUNT_SID": "ACxxxx"}
    )
    assert "twilio-sms" not in enabled
    assert "twilio-sms" not in ctx.channel_configs


def test_rehydrate_empty_env_is_inbox_only():
    store = InMemoryInboxStore()
    registry = ChannelAdapterRegistry()
    registry.register(InboxChannelAdapterProvider(store=store))
    ctx = SendContext(registry=registry, inbox_store=store)
    enabled = rehydrate_from_env(ctx, env={})
    assert enabled == []
    assert ctx.registry.names() == ["inbox"]
