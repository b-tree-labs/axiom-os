# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0

"""Slack Socket-Mode provider (ADR-074 Phase 2): pure helpers (Block Kit
build + event parsing) tested without slack_sdk; the live websocket glue is
exercised only when the SDK is installed."""

from __future__ import annotations

from axiom.extensions.builtins.notifications.channels.interactive import (
    ApprovalOption,
    ApprovalOutcome,
    ApprovalRequest,
    ChannelMessage,
    InteractiveChannel,
)
from axiom.extensions.builtins.notifications.channels.slack_interactive import (
    SlackInteractiveChannel,
    build_approval_blocks,
    parse_slack_event,
)


def test_approval_blocks_render_buttons_with_ids_and_styles():
    req = ApprovalRequest(
        prompt="Approve?",
        options=(
            ApprovalOption("approve", "Approve & apply", style="primary"),
            ApprovalOption("deny", "Deny", style="danger"),
        ),
    )
    blocks = build_approval_blocks(req)
    actions = [b for b in blocks if b["type"] == "actions"][0]
    btns = {b["action_id"]: b for b in actions["elements"]}
    assert set(btns) == {"approve", "deny"}
    assert btns["approve"]["style"] == "primary"
    assert btns["deny"]["style"] == "danger"
    assert btns["approve"]["text"]["text"] == "Approve & apply"


def test_parse_human_message():
    ev = {"type": "message", "text": "what is the limit?", "user": "U1", "ts": "1.1", "thread_ts": "1.0"}
    out = parse_slack_event(ev)
    assert isinstance(out, ChannelMessage)
    assert out.text == "what is the limit?"
    assert out.author == "U1"
    assert out.thread_id == "1.0"
    assert out.is_agent is False


def test_parse_bot_message_is_marked_agent():
    ev = {"type": "message", "text": "hi", "user": "U2", "bot_id": "B1", "ts": "2.1"}
    out = parse_slack_event(ev)
    assert isinstance(out, ChannelMessage)
    assert out.is_agent is True  # so the workflow ignores its own posts


def test_parse_block_action_to_approval_outcome():
    ev = {
        "type": "block_actions",
        "user": {"id": "U9"},
        "actions": [{"action_id": "approve"}],
        "container": {"thread_ts": "5.0"},
    }
    out = parse_slack_event(ev)
    assert isinstance(out, ApprovalOutcome)
    assert out.action_id == "approve"
    assert out.actor == "U9"
    assert out.thread_id == "5.0"


def test_parse_unknown_event_is_none():
    assert parse_slack_event({"type": "reaction_added"}) is None


def test_provider_satisfies_interface_and_posts_via_injected_client():
    sent = []

    class FakeWebClient:
        def chat_postMessage(self, **kw):
            sent.append(kw)
            return {"ts": kw.get("thread_ts") or "100.1"}

    ch = SlackInteractiveChannel(
        bot_token="xoxb-x", app_token="xapp-x", channel="C123", web_client=FakeWebClient()
    )
    assert isinstance(ch, InteractiveChannel)
    tid = ch.post("hello")
    assert sent[0]["channel"] == "C123"
    assert sent[0]["text"] == "hello"
    assert "username" not in sent[0]  # default author -> no override
    assert tid == "100.1"

    # Agent attribution: the author (AgentCard.name) surfaces as the Slack username.
    ch.post("from TRIAGE", author="TRIAGE")
    assert sent[1]["username"] == "TRIAGE"

    ch.request_approval(ApprovalRequest(prompt="ok?", options=(ApprovalOption("approve", "Yes"),), thread_id="100.1"))
    assert "blocks" in sent[2]
