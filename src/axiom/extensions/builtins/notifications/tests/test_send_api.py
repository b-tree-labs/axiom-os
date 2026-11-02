# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Public send() façade contract.

The SEC-1 scope: classification routing (§4 of spec) + inbox adapter
dispatch + receipt write. Real channel adapters land in HERALD-2.
"""

from __future__ import annotations

from axiom.extensions.builtins.notifications.channels.base import (
    ChannelAdapterRegistry,
)
from axiom.extensions.builtins.notifications.channels.inbox import (
    InboxChannelAdapterProvider,
)
from axiom.extensions.builtins.notifications.inbox import InMemoryInboxStore
from axiom.extensions.builtins.notifications.send import (
    NotificationPayload,
    Priority,
    SendContext,
    send,
)
from axiom.governance import Classification


def _ctx() -> tuple[SendContext, InMemoryInboxStore]:
    store = InMemoryInboxStore()
    registry = ChannelAdapterRegistry()
    registry.register(InboxChannelAdapterProvider(store=store))
    return SendContext(registry=registry, inbox_store=store), store


def test_send_to_inbox_writes_receipt_and_inbox_row() -> None:
    ctx, store = _ctx()
    receipt = send(
        ctx,
        actor="@agent:test",
        recipient="@jim:test",
        payload=NotificationPayload(summary="sample transition"),
        classification=Classification.INTERNAL,
        priority=Priority.NORMAL,
        intent="test.notify",
    )
    assert receipt.outcome == "succeeded"
    assert receipt.channel_selected == "inbox"
    assert len(store.all()) == 1


def test_send_respects_classification_routing() -> None:
    """A CONTROLLED envelope must not route through an INTERNAL-ceiling adapter.

    With only the inbox adapter (CONTROLLED ceiling) registered the send
    succeeds; we verify the rationale records the per-candidate decision.
    """
    ctx, _store = _ctx()
    receipt = send(
        ctx,
        actor="@agent:test",
        recipient="@rso:test",
        payload=NotificationPayload(summary="controlled-class alert"),
        classification=Classification.CONTROLLED,
        priority=Priority.HIGH,
        intent="test.notify",
    )
    assert receipt.outcome == "succeeded"
    assert receipt.channel_selected == "inbox"
    assert receipt.routing_rationale
    admitted = [r for r in receipt.routing_rationale if r["admitted"]]
    assert {r["adapter"] for r in admitted} == {"inbox"}


def test_send_denies_when_no_admitted_channel() -> None:
    """With NO adapters registered (not even inbox), CONTROLLED send denies."""
    registry = ChannelAdapterRegistry()
    store = InMemoryInboxStore()
    ctx = SendContext(registry=registry, inbox_store=store)
    receipt = send(
        ctx,
        actor="@agent:test",
        recipient="@rso:test",
        payload=NotificationPayload(summary="x"),
        classification=Classification.CONTROLLED,
        priority=Priority.HIGH,
        intent="test.notify",
    )
    assert receipt.outcome == "denied"
    assert receipt.channel_selected is None


def test_dedup_returns_prior_receipt() -> None:
    ctx, _store = _ctx()
    r1 = send(
        ctx,
        actor="@agent:test",
        recipient="@jim:test",
        payload=NotificationPayload(summary="hi"),
        classification=Classification.INTERNAL,
        priority=Priority.NORMAL,
        intent="test.notify",
        dedup_key="key-1",
    )
    r2 = send(
        ctx,
        actor="@agent:test",
        recipient="@jim:test",
        payload=NotificationPayload(summary="hi again"),
        classification=Classification.INTERNAL,
        priority=Priority.NORMAL,
        intent="test.notify",
        dedup_key="key-1",
    )
    assert r1.id == r2.id
