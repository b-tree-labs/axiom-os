# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0

"""Human reply → chat-agent → in-thread answer, with reply-bind-back
(ADR-067 §3, ADR-074).

The wire that turns a human message observed on *any* ``InteractiveChannel``
(Teams, Slack, the in-memory test channel) into a grounded answer from the
AXI chat agent, posted back in-thread. It is vendor-neutral by design:
it speaks only to the ``InteractiveChannel`` protocol and the reply-bind-back
transport in ``gateway/threads.py``.

Flow of one inbound human message:

  1. recover the correlation token in the text (``threads.parse_token``) and
     look up the originating actor/alert in the ``ThreadStore`` — the bind-back
     that ties a reply to the agent that raised it;
  2. hand the *cleaned* text (our correlation footer stripped) to the
     injectable ``responder`` — default = the real LLM+RAG ``ChatAgent`` —
     with the bound context;
  3. mint a fresh correlation, re-bind it so the human's next reply routes
     back to this agent, and post the answer in-thread with a fresh footer
     (``threads.embed_footer``).

The ``responder`` is the seam: default points at the configured LLM+RAG chat
agent; tests inject a fake. Nothing here imports the chat engine at module load —
the default responder lazy-constructs the agent on first use.
"""

from __future__ import annotations

import re
from collections.abc import Callable

from ..channels.interactive import ChannelMessage, InteractiveChannel
from .threads import ThreadStore, embed_footer, mint_correlation_id, parse_token

# responder(question, context) -> answer text.  ``context`` carries the
# bind-back result (originating actor, correlation id, thread) when resolved.
ChatResponder = Callable[[str, dict], str]

# AEOS §3.2: agents use the ALL-CAPS-HYPHEN AXI convention (AXI, SCAN, TIDY,
# …). A product or site name is not an agent — the shipped conversational
# assistant is AXI (chat extension, agents/axi/persona.md).
_DEFAULT_AGENT = "AXI"
_DEFAULT_AGENT_PRINCIPAL = "@axi"

# Strip our own reply-bind-back footer before the agent sees the text, so the
# model gets the bare question, not the internal token. Same token shape as
# ``threads._FOOTER`` (kept local so ``threads.py`` stays reused verbatim).
_FOOTER_RE = re.compile(r"\s*\[axi-corr:\s*[0-9a-f]{8}\]\s*")


def strip_footer(text: str) -> str:
    return _FOOTER_RE.sub(" ", text or "").strip()


def default_chat_responder() -> ChatResponder:
    """The production responder: the configured LLM+RAG ``ChatAgent``.

    Lazily builds one ``ChatAgent`` (its own Gateway resolves the configured
    provider, and RAG grounding happens inside ``.turn``) and reuses it. Import
    is deferred so this module loads without the chat extension present.
    """
    agent_box: list = []

    def _respond(question: str, context: dict) -> str:
        if not agent_box:
            from axiom.extensions.builtins.chat.agent import ChatAgent

            agent_box.append(ChatAgent())
        agent = agent_box[0]
        # Non-streaming single turn: RAG context + tools are assembled inside
        # ChatAgent.turn; the return value is the assistant's final text.
        return agent.turn(question, stream=False)

    return _respond


def attach_chat_agent(
    channel: InteractiveChannel,
    *,
    responder: ChatResponder | None = None,
    threads: ThreadStore | None = None,
    agent: str = _DEFAULT_AGENT,
    agent_principal: str = _DEFAULT_AGENT_PRINCIPAL,
    vendor: str = "teams",
) -> Callable[[ChannelMessage], None]:
    """Register the human-reply → chat-agent handler on ``channel``.

    Returns the registered handler (handy for direct unit invocation). Skips
    the agent's own posts (loop guard). Binds every answer's fresh correlation
    into ``threads`` so a follow-up reply routes back to this agent.
    """
    responder = responder or default_chat_responder()

    def _on_message(msg: ChannelMessage) -> None:
        if msg.is_agent:
            return  # never answer our own post (feedback-loop guard)

        # 1. reply-bind-back: recover the originating actor for this thread.
        context: dict = {"thread_id": msg.thread_id, "vendor": vendor}
        if threads is not None:
            token = parse_token(msg.text)
            if token:
                rec = threads.by_token(token)
                if rec is not None:
                    context["actor"] = rec.actor
                    context["correlation_id"] = rec.correlation_id

        # 2. hand the cleaned question to the (injectable) chat agent.
        answer = responder(strip_footer(msg.text), context)
        if not answer:
            return

        # 3. mint + re-bind a fresh correlation, then post the answer with a
        #    forward-surviving footer so the next reply binds back here.
        cid = mint_correlation_id()
        if threads is not None:
            threads.bind(
                cid,
                actor=context.get("actor", agent_principal),
                vendor=vendor,
                thread_ref=msg.thread_id,
            )
        channel.post(
            embed_footer(answer, cid),
            thread_id=msg.thread_id,
            author=agent,
        )

    channel.on_message(_on_message)
    return _on_message


__all__ = [
    "ChatResponder",
    "default_chat_responder",
    "attach_chat_agent",
    "strip_footer",
]
