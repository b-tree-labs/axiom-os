# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Skill functions — the shared code path behind the CLI and the MCP tools."""

from __future__ import annotations

from axiom.extensions.builtins.principal.skills import route as route_skill
from axiom.extensions.builtins.principal.skills import setup as setup_skill
from axiom.extensions.builtins.principal.skills import status as status_skill
from axiom.extensions.builtins.principal.skills import verify as verify_skill

ANSWERS = {
    "display_name": "Test Principal",
    "endpoints": [
        {"kind": "chat", "address": "room:ops"},
        {"kind": "email", "address": "p@example.test"},
    ],
    "preferences": [
        {"topic_class": "incident", "ranked_kinds": ["chat", "email"], "urgency_floor": 3},
        {"topic_class": "*", "ranked_kinds": ["email"]},
    ],
}


def _ok_sender(receipts):
    def _send(endpoint, message):
        return receipts.get(endpoint.kind)

    return _send


def test_status_on_a_fresh_harness_says_absent_and_what_to_do(tmp_path):
    r = status_skill.run({"config_dir": tmp_path}, None)
    assert r.ok and r.value["status"] == "absent"
    assert "axi principal setup" in r.actions_taken[0]


def test_setup_returns_the_next_question_rather_than_guessing(tmp_path):
    r = setup_skill.run({"config_dir": tmp_path, "answers": {"display_name": "X"}}, None)
    assert r.ok and r.value["complete"] is False
    assert r.value["next_question"]["key"] == "endpoints"


def test_setup_completes_and_verifies(tmp_path):
    r = setup_skill.run(
        {
            "config_dir": tmp_path,
            "answers": ANSWERS,
            "handle": "@ben:netl",
            "send": _ok_sender({"chat": "rcpt-1", "email": "rcpt-2"}),
        },
        None,
    )
    assert r.ok and r.value["verified"] is True
    assert r.value["status"] == "active"


def test_setup_rejects_a_handle_that_is_not_platform_grammar(tmp_path):
    r = setup_skill.run(
        {"config_dir": tmp_path, "answers": ANSWERS, "handle": "p@example.test"}, None
    )
    assert not r.ok and "@" in r.errors[0]


def test_skipping_verification_leaves_the_harness_unverified(tmp_path):
    r = setup_skill.run(
        {"config_dir": tmp_path, "answers": ANSWERS, "handle": "@ben:netl", "skip_verify": True},
        None,
    )
    assert r.value["verified"] is False
    assert r.value["status"] == "unverified"
    after = status_skill.run({"config_dir": tmp_path}, None)
    assert all(not e["verified"] for e in after.value["endpoints"])


def test_a_channel_that_sends_nothing_is_not_counted_as_working(tmp_path):
    setup_skill.run(
        {"config_dir": tmp_path, "answers": ANSWERS, "handle": "@ben:netl", "skip_verify": True},
        None,
    )
    r = verify_skill.run(
        {"config_dir": tmp_path, "send": _ok_sender({"chat": None, "email": "rcpt-2"})}, None
    )
    assert r.ok, "one good channel is still a usable principal"
    assert r.value["verified"] == ["email"]
    assert r.value["degraded"] == ["chat"]
    assert r.value["fully_verified"] is False


def test_route_skill_explains_and_skips_the_unproven_channel(tmp_path):
    setup_skill.run(
        {
            "config_dir": tmp_path,
            "answers": ANSWERS,
            "handle": "@ben:netl",
            "send": _ok_sender({"chat": None, "email": "rcpt-2"}),
        },
        None,
    )
    r = route_skill.run({"config_dir": tmp_path, "topic": "incident", "urgency": 9}, None)
    assert r.ok
    assert r.value["chosen"] == "email", "chat never round-tripped, so it is not a channel"
    assert "chat" in r.value["rejected"]
    assert r.value["explain"]


def test_route_without_a_principal_fails_loudly(tmp_path):
    r = route_skill.run({"config_dir": tmp_path, "topic": "incident"}, None)
    assert not r.ok and "setup" in r.errors[0]
