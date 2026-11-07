# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0

"""PR-9: reply-bind-back correlation + TRIAGE deterministic classifier."""

from __future__ import annotations

from axiom.extensions.builtins.notifications.gateway.classify import classify_inbound
from axiom.extensions.builtins.notifications.gateway.threads import (
    ThreadStore,
    body_token,
    embed_footer,
    mint_correlation_id,
    parse_token,
)
from axiom.extensions.builtins.notifications.gateway.triage import register_triage
from axiom.infra.bus.event_bus import EventBus

KNOWN = ["rivet", "tidy", "scan", "press", "pulse", "keep"]


# --- reply-bind-back correlation ------------------------------------------ #
def test_token_is_stable_and_8_hex():
    corr = mint_correlation_id()
    t = body_token(corr)
    assert len(t) == 8
    assert body_token(corr) == t  # deterministic


def test_embed_then_parse_roundtrip():
    corr = mint_correlation_id()
    out = embed_footer("RIVET: build is green", corr)
    assert body_token(corr) in out
    # survives a quoted-reply prefix (forwarded text)
    forwarded = "> " + out + "\n\nthanks, looks good"
    assert parse_token(forwarded) == body_token(corr)


def test_parse_token_absent():
    assert parse_token("no token here") is None


def test_threadstore_bind_and_lookup():
    store = ThreadStore()
    corr = mint_correlation_id()
    rec = store.bind(corr, actor="@rivet:bens", vendor="slack", thread_ref="123.45")
    assert store.by_token(body_token(corr)).actor == "@rivet:bens"
    assert store.by_correlation(corr) is rec


# --- classifier: mention wins --------------------------------------------- #
def test_mention_resolves_target():
    d = classify_inbound("@rivet rebase PR 12", known_agents=KNOWN)
    assert d.target_principal == "@rivet"
    assert d.reason == "mention"
    assert d.confidence == 1.0


def test_unknown_mention_is_ignored():
    d = classify_inbound("@nobody do thing", known_agents=KNOWN)
    assert d.target_principal is None
    assert d.reason == "below_floor"


def test_slack_bot_mention_does_not_match_agent():
    # Slack's <@U0BOT> parses to @U0BOT — not a known agent → no false match.
    d = classify_inbound("<@U0BOT> hello", known_agents=KNOWN)
    assert d.reason == "below_floor"


def test_first_known_mention_wins():
    d = classify_inbound("@tidy and @scan", known_agents=KNOWN)
    assert d.target_principal == "@tidy"


# --- classifier: thread-context fallback ---------------------------------- #
def test_thread_context_routes_to_original_actor():
    store = ThreadStore()
    corr = mint_correlation_id()
    store.bind(corr, actor="@rivet:bens", vendor="slack")
    reply = embed_footer("(no mention here)", corr)
    d = classify_inbound(reply, known_agents=KNOWN, threads=store)
    assert d.target_principal == "@rivet:bens"
    assert d.reason == "thread"


def test_mention_beats_thread_context():
    store = ThreadStore()
    corr = mint_correlation_id()
    store.bind(corr, actor="@rivet:bens", vendor="slack")
    reply = embed_footer("@tidy take this instead", corr)
    d = classify_inbound(reply, known_agents=KNOWN, threads=store)
    assert d.target_principal == "@tidy"
    assert d.reason == "mention"


def test_below_floor_has_reply_text():
    d = classify_inbound("just chatting", known_agents=KNOWN)
    assert d.reply_text and "mention" in d.reply_text.lower()


# --- TRIAGE bus subscriber ------------------------------------------------- #
def test_register_triage_emits_dispatch_on_mention():
    bus = EventBus()
    seen = []
    bus.subscribe("herald.dispatch.>", lambda s, p: seen.append((s, p)))
    register_triage(bus, known_agents=KNOWN)

    bus.publish(
        "herald.inbound.slack",
        payload={"text": "@rivet status", "vendor": "slack", "thread_ref": "9.9"},
    )
    assert len(seen) == 1
    subject, payload = seen[0]
    assert subject == "herald.dispatch.rivet"
    assert payload["target"] == "@rivet"
    assert payload["thread_ref"] == "9.9"


def test_register_triage_emits_reply_below_floor():
    bus = EventBus()
    replies = []
    bus.subscribe("herald.reply", lambda s, p: replies.append(p))
    register_triage(bus, known_agents=KNOWN)

    bus.publish("herald.inbound.slack", payload={"text": "hello there", "vendor": "slack"})
    assert len(replies) == 1
    assert "mention" in replies[0]["reply_text"].lower()


def test_dispatch_payload_carries_channel_for_reply():
    bus = EventBus()
    seen = []
    bus.subscribe("herald.dispatch.>", lambda s, p: seen.append(p))
    register_triage(bus, known_agents=KNOWN)
    bus.publish(
        "herald.inbound.slack",
        payload={"text": "@rivet status", "vendor": "slack",
                 "channel": "C0ROOM", "thread_ref": "1.2"},
    )
    assert seen[0]["channel"] == "C0ROOM"
    assert seen[0]["thread_ref"] == "1.2"
