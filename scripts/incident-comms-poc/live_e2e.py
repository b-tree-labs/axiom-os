#!/usr/bin/env python3
# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0
"""Live agent-comms presence — a bounded, no-hardcode agent loop.

Axi (the configured LLM) interprets natural language and calls its tools; there
is no keyword routing, no canned intents, no hardcoded task or copy. The tool
surface is **bounded** to the agent's own orchestration tools (see chat
tools_ext/agent_ops), so a headless channel presence can't reach write_file etc.
Tool calls are resolved native-first with a shim fallback (axiom.llm.tool_calling)
so it works even on models that don't emit native tool_calls (e.g. Qwen on
a self-hosted node). Channel-agnostic: Slack and SMS entrypoints just pass a channel.
"""
from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))

from axiom.extensions.builtins.chat.tools import execute_tool  # noqa: E402
from axiom.extensions.builtins.chat.tools_ext.agent_ops import TOOLS as _OPS  # noqa: E402
from axiom.extensions.builtins.chat.tools_ext.agent_ops import set_op_context  # noqa: E402
from axiom.extensions.builtins.connect.presence import (  # noqa: E402
    DEFAULT_PRESENCE_BRIEF,
    presence_display_name,
)
from axiom.llm.gateway import Gateway  # noqa: E402
from axiom.llm.tool_calling import ToolCall, resolve_tool_calls  # noqa: E402

PRINCIPAL = os.environ.get("AXI_PRINCIPAL", "@axi:bens")
OWNER = os.environ.get("AXI_OWNER") or None  # the principal that controls the work
_OPS_DEFS = [{"type": "function", "function": {"name": t.name, "description": t.description,
                                               "parameters": t.parameters}} for t in _OPS]
_OPS_NAMES = {t.name for t in _OPS}

SYSTEM = (
    DEFAULT_PRESENCE_BRIEF
    + " You have tools to report what long-running work you're running, stop it, verify a "
    "measured value against a digital-twin prediction, and delegate to a subject-matter agent "
    "(e.g. tidy, triage). Use a tool when the operator's request calls for it; otherwise just "
    "talk. Speak in natural human prose — direct but polite, plainspoken, concise (a sentence "
    "or two). Never machine-like status dumps, and never overly chipper or superfluous."
)


def agent_name() -> str:
    try:
        return presence_display_name(PRINCIPAL)
    except Exception:
        return "Axi"


def _run_turn(gw, provider, history, *, runner=None, max_steps=4) -> str:
    """One bounded agent turn: let Axi call its tools (native-or-shim) until it
    answers in prose. Tool scaffolding stays turn-local; only the reply persists."""
    msgs = list(history)
    for _ in range(max_steps):
        def native_fn():
            r = gw.complete_with_tools(messages=msgs, system=SYSTEM, tools=_OPS_DEFS)
            calls = [ToolCall(b.name, b.input or {}) for b in (r.tool_use or [])]
            return calls, (r.text or "")

        def complete_fn(extra_system: str) -> str:
            r = gw.complete_with_tools(messages=msgs, system=SYSTEM + "\n\n" + extra_system, tools=None)
            return r.text or ""

        res = resolve_tool_calls(_OPS_DEFS, native_fn=native_fn, complete_fn=complete_fn,
                                 mode=getattr(provider, "tool_mode", "auto"), provider=provider.name)
        if not res.calls:
            return res.text or "Sorry — I didn't catch that. Could you rephrase?"
        for c in res.calls:
            result = execute_tool(c.name, c.arguments) if c.name in _OPS_NAMES \
                else {"error": f"{c.name} is not one of my tools"}
            msgs.append({"role": "assistant", "content": f"[called {c.name}({json.dumps(c.arguments)})]"})
            msgs.append({"role": "user", "content": f"Result of {c.name}: {json.dumps(result)}. "
                         "Reply to the operator in natural prose; don't call another tool unless needed."})
    return "I went a few rounds without landing an answer — could you rephrase?"


def wire(channel, *, runner=None):
    """Register the agent loop on a channel. Returns (intro, gateway)."""
    gw = Gateway()
    provider = gw.providers[0] if gw.providers else None
    history: list[dict] = []

    def post(text: str) -> None:
        channel.post(text, author=agent_name())

    def on_message(msg) -> None:
        if getattr(msg, "is_agent", False) or not msg.text.strip():
            return
        set_op_context(principal=PRINCIPAL, owner=OWNER or msg.author,
                       requester=msg.author, runner=runner)
        history.append({"role": "user", "content": msg.text})
        try:
            reply = _run_turn(gw, provider, history, runner=runner)
        except Exception as exc:  # noqa: BLE001
            reply = f"(I hit an error reaching the model: {exc})"
        history.append({"role": "assistant", "content": reply})
        post(reply)

    channel.on_message(on_message)
    intro = f"{agent_name()} here, online on this node. Ask me anything, or check in on what I'm running."
    return intro, gw
