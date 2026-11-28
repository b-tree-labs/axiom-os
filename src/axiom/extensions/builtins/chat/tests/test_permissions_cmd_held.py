# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""``/permissions``: the help matches the verbs, and the hold-for-human queue is visible.

An action held with ``h`` goes to the durable queue ``axi approve`` reads. Before this, chat could put
actions there and offer no way to see them, so the person who pressed ``h`` had to leave the chat to
learn what was waiting.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from axiom.extensions.builtins.chat.agent import _hold_for_later
from axiom.extensions.builtins.chat.builtin_commands import ensure_loaded
from axiom.extensions.builtins.chat.command_registry import REGISTRY
from axiom.infra.orchestrator.actions import Action, ActionCategory
from axiom.infra.orchestrator.approval import ApprovalGate
from axiom.infra.orchestrator.approval_store import FileActionStore, default_approval_path

from ._tools_cmd_helpers import make_agent, run


def _held(name="doc_publish", age_hours=0.0, **params):
    a = Action(name=name, params=params or {"source": "a.md"}, category=ActionCategory.WRITE)
    a.created_at = (datetime.now(UTC) - timedelta(hours=age_hours)).isoformat()
    assert _hold_for_later(a) == a.action_id
    return a


# ---- the registry entry matches the implemented verbs ----------------------------------------


def test_registry_entry_advertises_every_verb_the_handler_implements():
    ensure_loaded()
    c = REGISTRY.get("/permissions")
    assert {"set", "reset", "held"} <= set(c.subcommands)
    for verb in ("set", "reset", "held"):
        assert verb in c.summary or verb in c.usage


@pytest.mark.parametrize("verb", ["set", "reset", "held"])
def test_every_advertised_verb_is_handled_not_reported_unknown(verb):
    ensure_loaded()
    assert verb in REGISTRY.get("/permissions").subcommands
    args = {"set": "write_file allow", "reset": "", "held": ""}[verb]
    out = run(f"/permissions {verb} {args}".strip())
    assert "Usage" not in out and "Unknown" not in out


def test_an_unknown_verb_prints_usage_that_names_all_verbs():
    out = run("/permissions frobnicate")
    assert "set" in out and "reset" in out and "held" in out


def test_set_and_reset_still_work():
    agent = make_agent()
    assert "write_file" in run("/permissions set write_file deny", agent)
    assert agent.permissions.get("write_file") == "deny"
    run("/permissions reset write_file", agent)
    assert agent.permissions.get("write_file") == "ask"


# ---- the held queue -------------------------------------------------------------------------


def test_listing_shows_a_held_action_with_id_and_age_and_how_to_approve(monkeypatch):
    a = _held(age_hours=3)
    out = run("/permissions")
    assert a.action_id in out and "doc_publish" in out
    assert "3h ago" in out
    assert "approve ok <id>" in out and "approve no <id>" in out


def test_held_verb_shows_only_the_queue():
    a = _held()
    out = run("/permissions held")
    assert a.action_id in out
    assert "Tool permissions" not in out


def test_empty_queue_says_nothing_is_waiting():
    assert "nothing is waiting" in run("/permissions held")


def test_answered_actions_are_not_listed_as_waiting():
    a = _held()
    ApprovalGate(FileActionStore(default_approval_path())).approve(a.action_id, decided_by="@t")
    assert a.action_id not in run("/permissions held")


def test_an_unreadable_queue_is_reported_as_unreadable_not_empty():
    p = default_approval_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("{not json")
    out = run("/permissions held")
    assert "nothing is waiting" not in out
    assert "cannot" in out.lower() or "could not" in out.lower()


def test_params_that_could_hold_secrets_are_not_printed():
    _held(name="send_note", token="s3cr3t-value", dest="x")
    out = run("/permissions held")
    assert "s3cr3t-value" not in out
    assert "token" in out  # the name of the argument is shown, never its value


def test_approve_hint_uses_the_brand_cli(monkeypatch):
    from axiom.infra import branding

    class _B:
        cli_name = "zed"

    monkeypatch.setattr(branding, "get_branding", lambda: _B())
    _held()
    out = run("/permissions held")
    assert "zed approve" in out and "axi approve" not in out
