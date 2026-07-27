#!/usr/bin/env python3
# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0
"""Standalone Axi presence in a Slack channel — talk to the agent about anything.

Unlike run_poc.py (one incident, approval-bound), this is a plain conversational
presence: it listens on the channel and replies to every human message or
@mention via the Axiom chat agent (Qwen on a self-hosted node, per runtime/config). This is
the "agent in a channel you can just talk to" litmus test.

Persona (per the identity rule): the agent speaks as the domain-consumer persona
when its answer draws on the domain RAG, otherwise as **Axi**. RAG is not wired into this
minimal presence yet, so it speaks as Axi; the switch is a one-line hook below.

Env: SLACK_BOT_TOKEN, SLACK_APP_TOKEN, SLACK_CHANNEL, PRIVATE_LLM_API_KEY.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))

from axiom.llm.gateway import Gateway  # noqa: E402
from axiom.extensions.builtins.connect.presence import (  # noqa: E402
    DEFAULT_PRESENCE_BRIEF,
    PresenceConfig,
    build_presence_agent,
    persona_for,
    presence_display_name,
)
from axiom.extensions.builtins.notifications.channels.interactive import ChannelMessage  # noqa: E402
from axiom.extensions.builtins.notifications.channels.slack_interactive import (  # noqa: E402
    SlackInteractiveChannel,
)

AGENT_ICON = os.environ.get("AXI_PRESENCE_ICON") or None
PRESENCE_BRIEF = DEFAULT_PRESENCE_BRIEF

# kept as a module alias for back-compat with anything importing _persona
_persona = persona_for


def main() -> int:
    for var in ("SLACK_BOT_TOKEN", "SLACK_APP_TOKEN", "SLACK_CHANNEL", "PRIVATE_LLM_API_KEY"):
        if not os.environ.get(var):
            print(f"ERROR: {var} not set", file=sys.stderr)
            return 1

    gateway = Gateway()
    if not gateway.available:
        print("ERROR: no LLM provider available (check runtime/config/models.toml + PRIVATE_LLM_API_KEY)", file=sys.stderr)
        return 1
    # Single construction seam (connect/presence.py) — identity/brief/mode bind here.
    # Bind the presence to a principal (e.g. @axi:bens) accountable to a human,
    # so memory writes carry real provenance and the channel brands per-owner.
    principal = os.environ.get("AXI_PRINCIPAL", "@axi:local")
    human = os.environ.get("AXI_ACCOUNTABLE_HUMAN") or None
    agent = build_presence_agent(
        PresenceConfig(brief=PRESENCE_BRIEF),
        principal=principal,
        accountable_human_id=human,
        gateway=gateway,
    )

    channel = SlackInteractiveChannel(
        bot_token=os.environ["SLACK_BOT_TOKEN"],
        app_token=os.environ["SLACK_APP_TOKEN"],
        channel=os.environ["SLACK_CHANNEL"],
    )

    seeded = {"done": False}

    def on_message(msg: ChannelMessage) -> None:
        if msg.is_agent or not msg.text.strip():
            return
        print(f"\n[in]  {msg.author}: {msg.text}", flush=True)
        # Seed identity into the first turn — instruct models honor an in-turn
        # directive far more reliably than a system-prompt persona alone.
        text = msg.text
        if not seeded["done"]:
            text = (PRESENCE_BRIEF + "\n\nAcknowledge that identity implicitly (do not "
                    "restate this brief), then answer:\n" + msg.text)
            seeded["done"] = True
        try:
            reply = agent.turn(text, stream=False, raw=True)
        except Exception as exc:  # noqa: BLE001 — surface, never crash the listener
            reply = f"(error reaching the model: {exc})"
        rag_used = bool(getattr(agent, "_last_retrieved", None))
        who = presence_display_name(principal, rag_used=rag_used)  # "Ben's Axi"
        print(f"[out] {who}: {reply}", flush=True)
        channel.post(reply or "(no response)", thread_id=msg.thread_id,
                     author=who, icon_url=AGENT_ICON)

    channel.on_message(on_message)
    print(f"Axi presence live on {os.environ['SLACK_CHANNEL']} via {gateway.providers[0].name}. Ctrl-C to stop.")
    channel.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
