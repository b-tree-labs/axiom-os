# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Integration: send() consults the recipient-preferences store."""

from __future__ import annotations

import pytest

from axiom.extensions.builtins.notifications.preferences import (
    InMemoryRecipientPreferenceStore,
    RecipientChannel,
    RecipientProfile,
    set_default_store,
)
from axiom.extensions.builtins.notifications.send import (
    ChannelPreferences,
    NotificationPayload,
    Priority,
    SendContext,
    send,
)
from axiom.governance import Classification


@pytest.fixture()
def isolated_store():
    store = InMemoryRecipientPreferenceStore()
    set_default_store(store)
    yield store
    set_default_store(None)


def test_send_uses_recipient_profile_when_no_explicit_prefs(
    isolated_store,
) -> None:
    isolated_store.put(
        RecipientProfile(
            recipient="@bbooth",
            channels=(
                # slack not registered → resolver drops, falls to inbox.
                RecipientChannel("slack", "#alerts"),
                RecipientChannel("inbox", "@bbooth"),
            ),
        )
    )
    ctx = SendContext.default()
    receipt = send(
        ctx,
        actor="@cli:test",
        recipient="@bbooth",
        payload=NotificationPayload(summary="profile-resolved"),
        classification=Classification.INTERNAL,
        priority=Priority.NORMAL,
    )
    assert receipt.outcome == "succeeded"
    assert receipt.channel_selected == "inbox"


def test_send_without_profile_uses_admitted_candidates(isolated_store) -> None:
    ctx = SendContext.default()
    receipt = send(
        ctx,
        actor="@cli:test",
        recipient="@nobody",
        payload=NotificationPayload(summary="no-profile path"),
        classification=Classification.INTERNAL,
        priority=Priority.NORMAL,
    )
    assert receipt.outcome == "succeeded"
    assert receipt.channel_selected == "inbox"


def test_send_explicit_channel_prefs_wins_over_profile(isolated_store) -> None:
    isolated_store.put(
        RecipientProfile(
            recipient="@bbooth",
            channels=(RecipientChannel("inbox", "@bbooth"),),
        )
    )
    ctx = SendContext.default()
    receipt = send(
        ctx,
        actor="@cli:test",
        recipient="@bbooth",
        payload=NotificationPayload(summary="explicit prefs"),
        classification=Classification.INTERNAL,
        priority=Priority.NORMAL,
        channel_prefs=ChannelPreferences(ordered_channels=("inbox",)),
    )
    assert receipt.outcome == "succeeded"
