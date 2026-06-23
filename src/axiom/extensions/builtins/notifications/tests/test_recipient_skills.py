# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""SkillRegistry coverage for recipient verbs (ADR-056)."""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from axiom.extensions.builtins.notifications import skills as notif_skills
from axiom.extensions.builtins.notifications.preferences import (
    InMemoryRecipientPreferenceStore,
)
from axiom.infra.skills import SkillContext


@pytest.fixture()
def reg_and_ctx(tmp_path: Path):
    reg = notif_skills.bind_default()
    ctx = SkillContext(
        registry=reg,
        state_dir=tmp_path,
        logger=logging.getLogger("test"),
        user_prompt=None,
    )
    return reg, ctx


def test_recipient_verbs_are_registered_skills(reg_and_ctx) -> None:
    reg, _ = reg_and_ctx
    assert reg.has("notifications.recipient_set")
    assert reg.has("notifications.recipient_show")
    assert reg.has("notifications.recipient_list")


def test_recipient_set_skill_writes_profile(reg_and_ctx) -> None:
    reg, ctx = reg_and_ctx
    store = InMemoryRecipientPreferenceStore()
    result = reg.invoke(
        "notifications.recipient_set",
        {
            "recipient": "@bbooth",
            "channels": "slack=#alerts,inbox",
            "_store": store,
        },
        ctx,
    )
    assert result.ok, result.errors
    assert store.get("@bbooth") is not None


def test_recipient_set_validates_recipient(reg_and_ctx) -> None:
    reg, ctx = reg_and_ctx
    result = reg.invoke(
        "notifications.recipient_set",
        {"recipient": "no-at-sign", "channels": "inbox"},
        ctx,
    )
    assert not result.ok
    assert any("'@'" in e for e in result.errors)


def test_recipient_show_skill_missing_returns_error(reg_and_ctx) -> None:
    reg, ctx = reg_and_ctx
    store = InMemoryRecipientPreferenceStore()
    result = reg.invoke(
        "notifications.recipient_show",
        {"recipient": "@missing", "_store": store},
        ctx,
    )
    assert not result.ok
    assert any("no recipient profile" in e for e in result.errors)


def test_recipient_show_skill_returns_profile(reg_and_ctx) -> None:
    reg, ctx = reg_and_ctx
    store = InMemoryRecipientPreferenceStore()
    reg.invoke(
        "notifications.recipient_set",
        {"recipient": "@bbooth", "channels": "inbox", "_store": store},
        ctx,
    )
    result = reg.invoke(
        "notifications.recipient_show",
        {"recipient": "@bbooth", "_store": store},
        ctx,
    )
    assert result.ok
    assert result.value["recipient"] == "@bbooth"


def test_recipient_list_skill_returns_count(reg_and_ctx) -> None:
    reg, ctx = reg_and_ctx
    store = InMemoryRecipientPreferenceStore()
    for handle in ("@a", "@b"):
        reg.invoke(
            "notifications.recipient_set",
            {"recipient": handle, "channels": "inbox", "_store": store},
            ctx,
        )
    result = reg.invoke(
        "notifications.recipient_list", {"_store": store}, ctx
    )
    assert result.ok
    assert result.value["count"] == 2
