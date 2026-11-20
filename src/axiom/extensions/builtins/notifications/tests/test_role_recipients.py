# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""A role handle reaches whoever this node says holds the role.

Platform code addresses roles. Which person holds one is node configuration, so
no personal handle lands in the platform.
"""

from __future__ import annotations

import pytest

from axiom.extensions.builtins.notifications.preferences import (
    InMemoryRecipientPreferenceStore,
    RecipientChannel,
    RecipientProfile,
    resolve_role,
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


class Settings:
    def __init__(self, table: dict[str, str]) -> None:
        self.table = table

    def get(self, key: str, default: object = None) -> object:
        return self.table.get(key, default)


ROLE = {"notifications.roles.operator": "@person"}


def test_a_mapped_role_resolves():
    assert resolve_role("@operator", settings=Settings(ROLE)) == "@person"


def test_a_role_with_a_context_resolves():
    assert resolve_role("@operator:local", settings=Settings(ROLE)) == "@person"


def test_an_unmapped_handle_is_left_alone():
    assert resolve_role("@someone", settings=Settings(ROLE)) == "@someone"
    assert resolve_role("@operator", settings=Settings({})) == "@operator"


def test_a_mapping_that_is_not_a_handle_is_ignored():
    assert resolve_role("@operator", settings=Settings({"notifications.roles.operator": "person"})) == "@operator"


def test_a_settings_failure_keeps_the_handle(monkeypatch):
    class Broken:
        def get(self, key: str, default: object = None) -> object:
            raise RuntimeError("damaged")

    assert resolve_role("@operator", settings=Broken()) == "@operator"


def test_the_role_key_ships_empty_so_no_person_is_named():
    assert settings_store._DEFAULTS["notifications.roles.operator"] == ""


@pytest.fixture()
def profile_store():
    store = InMemoryRecipientPreferenceStore()
    set_default_store(store)
    yield store
    set_default_store(None)


def _send(recipient: str):
    return send(
        SendContext.default(),
        actor="@rivet",
        recipient=recipient,
        payload=NotificationPayload(summary="main is red"),
        classification=Classification.INTERNAL,
        priority=Priority.HIGH,
    )


def test_send_to_a_role_lands_in_the_holders_inbox(profile_store, monkeypatch):
    profile_store.put(RecipientProfile(recipient="@person", channels=(RecipientChannel("inbox", "@person"),)))
    monkeypatch.setattr(settings_store, "SettingsStore", lambda: Settings(ROLE))
    receipt = _send("@operator")
    assert receipt.recipient == "@person"


def test_send_to_an_unmapped_role_still_delivers(profile_store, monkeypatch):
    monkeypatch.setattr(settings_store, "SettingsStore", lambda: Settings({}))
    assert _send("@operator").recipient == "@operator"
