# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0

"""Teams (Bot Framework) provider for the vendor-neutral ``InteractiveChannel``
(ADR-074 Phase 2, HERALD-2b). Pure helpers (Adaptive-Card build + Activity
parsing) tested without ``botbuilder``; the REST post is exercised through an
injected poster + fake token provider (no network, no SDK)."""

from __future__ import annotations

from axiom.extensions.builtins.notifications.channels.interactive import (
    ApprovalOption,
    ApprovalOutcome,
    ApprovalRequest,
    ChannelMessage,
    InteractiveChannel,
)
from axiom.extensions.builtins.notifications.channels.teams_interactive import (
    TeamsInteractiveChannel,
    build_approval_card,
    parse_teams_activity,
)


# --- Adaptive-Card build --------------------------------------------------- #
def test_approval_card_renders_submit_actions_with_ids_and_styles():
    req = ApprovalRequest(
        prompt="Approve?",
        options=(
            ApprovalOption("approve", "Approve & apply", style="primary"),
            ApprovalOption("deny", "Deny", style="danger"),
        ),
    )
    msg = build_approval_card(req)
    assert msg["type"] == "message"
    card = msg["attachments"][0]["content"]
    assert card["type"] == "AdaptiveCard"
    actions = {a["data"]["action_id"]: a for a in card["actions"]}
    assert set(actions) == {"approve", "deny"}
    assert actions["approve"]["type"] == "Action.Submit"
    assert actions["approve"]["style"] == "positive"   # primary -> positive
    assert actions["deny"]["style"] == "destructive"   # danger  -> destructive
    assert actions["approve"]["title"] == "Approve & apply"


# --- Activity parsing ------------------------------------------------------ #
def test_parse_human_message():
    activity = {
        "type": "message",
        "text": "what is the rod limit?",
        "from": {"id": "29:user-aad-id", "name": "Ben"},
        "conversation": {"id": "19:room@thread.tacv2;messageid=1700"},
    }
    out = parse_teams_activity(activity)
    assert isinstance(out, ChannelMessage)
    assert out.text == "what is the rod limit?"
    assert out.author == "29:user-aad-id"
    assert out.thread_id == "19:room@thread.tacv2;messageid=1700"
    assert out.is_agent is False


def test_parse_message_strips_bot_at_mention():
    activity = {
        "type": "message",
        "text": "<at>AXI</at> what changed on unit 1?",
        "from": {"id": "u1"},
        "conversation": {"id": "19:room@thread.tacv2"},
        "replyToId": "1700",
    }
    out = parse_teams_activity(activity)
    assert isinstance(out, ChannelMessage)
    assert out.text == "what changed on unit 1?"  # mention stripped
    # bare conversation id + replyToId are spliced into a postable thread ref
    assert out.thread_id == "19:room@thread.tacv2;messageid=1700"


def test_parse_bot_authored_message_is_marked_agent():
    activity = {
        "type": "message",
        "text": "hi",
        "from": {"id": "28:bot-id", "role": "bot"},
        "conversation": {"id": "19:room@thread.tacv2"},
    }
    out = parse_teams_activity(activity)
    assert isinstance(out, ChannelMessage)
    assert out.is_agent is True  # so the workflow ignores its own posts


def test_parse_invoke_card_action_to_approval_outcome():
    activity = {
        "type": "invoke",
        "name": "adaptiveCard/action",
        "from": {"id": "29:approver"},
        "conversation": {"id": "19:room@thread.tacv2;messageid=1"},
        "value": {"action": {"type": "Action.Execute", "data": {"action_id": "approve"}}},
    }
    out = parse_teams_activity(activity)
    assert isinstance(out, ApprovalOutcome)
    assert out.action_id == "approve"
    assert out.actor == "29:approver"
    assert out.thread_id == "19:room@thread.tacv2;messageid=1"


def test_parse_message_card_submit_to_approval_outcome():
    # Action.Submit surfaces as a message Activity carrying the card data.
    activity = {
        "type": "message",
        "from": {"id": "29:approver"},
        "conversation": {"id": "19:room@thread.tacv2"},
        "value": {"action_id": "deny"},
    }
    out = parse_teams_activity(activity)
    assert isinstance(out, ApprovalOutcome)
    assert out.action_id == "deny"


def test_parse_non_message_activity_is_none():
    assert parse_teams_activity({"type": "conversationUpdate"}) is None
    assert parse_teams_activity({"type": "typing"}) is None


# --- TeamsInteractiveChannel: outbound over injected poster + token -------- #
class _FakeResp:
    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


class _FakePoster:
    def __init__(self):
        self.calls = []

    def post(self, url, json, headers, timeout):
        self.calls.append({"url": url, "json": json, "headers": headers})
        return _FakeResp({"id": "activity-42"})


def _channel(poster):
    return TeamsInteractiveChannel(
        app_id="app-guid",
        app_password="secret",
        service_url="https://smba.trafficmanager.net/amer/",
        conversation_id="19:default@thread.tacv2",
        poster=poster,
        token_provider=lambda: "fake-bearer-token",
    )


def test_channel_satisfies_interface_and_posts_via_injected_poster():
    poster = _FakePoster()
    ch = _channel(poster)
    assert isinstance(ch, InteractiveChannel)

    tid = ch.post("hello team")
    call = poster.calls[0]
    # trailing slash on service_url normalized; conversation id in the path
    assert call["url"] == (
        "https://smba.trafficmanager.net/amer/v3/conversations/"
        "19:default@thread.tacv2/activities"
    )
    assert call["headers"]["Authorization"] == "Bearer fake-bearer-token"
    assert call["json"]["type"] == "message"
    assert call["json"]["text"] == "hello team"
    assert tid == "activity-42"  # id echoed from the Connector response


def test_post_targets_thread_and_attributes_author():
    poster = _FakePoster()
    ch = _channel(poster)
    ch.post("answer", thread_id="19:other@thread.tacv2;messageid=9", author="AXI")
    call = poster.calls[0]
    assert "19:other@thread.tacv2;messageid=9/activities" in call["url"]
    # Bot posts under its own identity, so the agent name leads the body.
    assert call["json"]["text"].startswith("**AXI**")
    assert "answer" in call["json"]["text"]


def test_request_approval_posts_adaptive_card():
    poster = _FakePoster()
    ch = _channel(poster)
    ch.request_approval(
        ApprovalRequest(
            prompt="ok?",
            options=(ApprovalOption("approve", "Yes"),),
            thread_id="19:t@thread.tacv2",
        )
    )
    sent = poster.calls[0]["json"]
    att = sent["attachments"][0]
    assert att["contentType"] == "application/vnd.microsoft.card.adaptive"


def test_dispatch_routes_message_and_action_to_handlers():
    poster = _FakePoster()
    ch = _channel(poster)
    msgs, actions = [], []
    ch.on_message(msgs.append)
    ch.on_action(actions.append)

    ch.dispatch({
        "type": "message",
        "text": "<at>AXI</at> status?",
        "from": {"id": "u1"},
        "conversation": {"id": "19:t@thread.tacv2"},
    })
    ch.dispatch({
        "type": "invoke",
        "from": {"id": "u2"},
        "conversation": {"id": "19:t@thread.tacv2"},
        "value": {"action": {"data": {"action_id": "approve"}}},
    })
    ch.dispatch({"type": "typing"})  # ignored

    assert [m.text for m in msgs] == ["status?"]
    assert [a.action_id for a in actions] == ["approve"]
