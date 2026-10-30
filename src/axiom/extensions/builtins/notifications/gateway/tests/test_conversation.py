# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0

"""Human reply → chat-agent → in-thread answer, with reply-bind-back
(ADR-067 §3). Runs the whole choreography against the vendor-free
``InMemoryInteractiveChannel`` with a FAKE responder — zero credentials, no
network, no chat engine."""

from __future__ import annotations

from axiom.extensions.builtins.notifications.channels.interactive import (
    InMemoryInteractiveChannel,
)
from axiom.extensions.builtins.notifications.gateway.conversation import (
    attach_chat_agent,
    strip_footer,
)
from axiom.extensions.builtins.notifications.gateway.threads import (
    ThreadStore,
    embed_footer,
    mint_correlation_id,
)


def test_strip_footer_removes_correlation_token():
    cid = mint_correlation_id()
    text = embed_footer("what changed?", cid)
    assert "axi-corr" in text
    assert strip_footer(text) == "what changed?"


def test_human_reply_answered_in_thread_with_bindback():
    channel = InMemoryInteractiveChannel()
    threads = ThreadStore()

    # An earlier alert from @rivet minted a correlation and bound it; its
    # footer travels on the outbound text the human is replying to.
    cid = mint_correlation_id()
    rec = threads.bind(cid, actor="@rivet", vendor="teams", thread_ref="thread-7")

    seen: list[tuple[str, dict]] = []

    def fake_responder(question: str, context: dict) -> str:
        seen.append((question, context))
        return "Rod bank at 62% — nominal."

    attach_chat_agent(
        channel, responder=fake_responder, threads=threads, vendor="teams"
    )

    # Human replies in-thread, quoting the alert (so the footer rides along).
    human_text = embed_footer("is the rod bank ok?", cid)
    channel.inject_message(human_text, author="ben", thread_id="thread-7")

    # 1. responder saw the CLEAN question (our footer stripped) ...
    assert len(seen) == 1
    question, context = seen[0]
    assert question == "is the rod bank ok?"
    assert "axi-corr" not in question
    # ... and the bind-back resolved the originating actor.
    assert context["actor"] == "@rivet"
    assert context["correlation_id"] == cid

    # 2. the answer was posted back in the SAME thread, authored AXI.
    answers = [p for p in channel.posts if p.author == "AXI"]
    assert len(answers) == 1
    posted = answers[0]
    assert posted.thread_id == "thread-7"
    assert "Rod bank at 62%" in posted.text

    # 3. a FRESH correlation was embedded + re-bound so the next reply routes
    #    back to this conversation (reply-bind-back is a live loop).
    from axiom.extensions.builtins.notifications.gateway.threads import parse_token

    new_token = parse_token(posted.text)
    assert new_token is not None and new_token != rec.token
    assert threads.by_token(new_token) is not None


def test_agent_own_posts_are_not_answered():
    channel = InMemoryInteractiveChannel()
    calls: list[str] = []
    attach_chat_agent(
        channel, responder=lambda q, c: calls.append(q) or "x", threads=ThreadStore()
    )
    # is_agent=True inbound (the bot's own echo) must be skipped.
    from axiom.extensions.builtins.notifications.channels.interactive import (
        ChannelMessage,
    )

    channel._msg_handlers[0](ChannelMessage(text="loop", author="bot", is_agent=True))
    assert calls == []
