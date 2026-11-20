# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``/stats``: per-turn latency, tokens, tool calls, cost and provider (ADR-168)."""

from __future__ import annotations

import json

from ..command_registry import CliSpec, McpForm, command
from ..follow_ups import FollowUp, pick, result, with_json_follow_ups

#: A turn this slow (model time) earns a pointer to the trace.
SLOW_TURN_MS = 15_000

FOLLOW_UPS = {
    "price": FollowUp("/where", "name the provider and model that went unpriced, then price that entry"),
    "trace": FollowUp("/trace on", "show where a slow turn's time went (administrators)"),
    "usage": FollowUp("/usage", "token and cost for each model call"),
    "context": FollowUp("/context", "how full the context window is"),
}

PRICE_NOTE = (
    "To price a model, set cost_per_1k_input and cost_per_1k_output on its entry in "
    "llm-providers.toml; until then its turns stay unpriced, never $0."
)


def follow_up_keys(agent, rep: dict, *, surface: str = "repl") -> tuple[list[str], list[str]]:
    """``(keys, notes)``: which follow-ups this report earns, and the plain sentences that go with them."""
    keys: list[str] = []
    notes: list[str] = []
    totals = rep.get("totals", {})
    if totals.get("unpriced_turns"):
        keys.append("price")
        notes.append(PRICE_NOTE)
    slowest = next((t for t in rep.get("turns", []) if t["turn"] == rep.get("slowest_turn")), None)
    if slowest and (slowest.get("latency_ms") or 0) >= SLOW_TURN_MS:
        from ..roles import is_admin

        if is_admin(agent, surface=surface):
            if not (agent.session.context or {}).get("trace"):
                keys.append("trace")
        else:
            notes.append(
                f"Turn {slowest['turn']} was slow. The per-turn trace is for administrators; "
                "ask one to run /trace on, or nothing more can be shown from here yet."
            )
    keys += ["usage", "context"]
    return keys, notes


def stats_data(agent, *, surface: str = "repl") -> dict:
    """``/stats --json``, ``axi chat --stats --json`` and the ``axiom_chat__stats`` tool all return this."""
    from ..stats import stats_report

    rep = stats_report(agent)
    keys, notes = follow_up_keys(agent, rep, surface=surface)
    out = with_json_follow_ups(rep, pick("/stats", *keys))
    if notes:
        out["notes"] = notes
    return out


@command(
    "/stats",
    "Per-turn latency, tokens, tools, cost and provider",
    "Inspect",
    usage="/stats [--json]",
    follow_ups=FOLLOW_UPS,
    cli=CliSpec(
        ("--stats",), line="/stats", applies="headless", metavar="SESSION", session_arg=True, json=True,
        help="print a stored session's per-turn usage and exit",
    ),
    expose=frozenset({"chat", "cli", "mcp"}),
    mcp=(McpForm("chat.stats"),),
)
def stats_command(ctx, args):
    from ..stats import render_stats, stats_report

    if "--json" in args:
        return json.dumps(stats_data(ctx.agent, surface=ctx.surface), indent=2)
    text = render_stats(ctx.agent)
    rep = stats_report(ctx.agent)
    if not rep["turns"]:
        return text
    keys, notes = follow_up_keys(ctx.agent, rep, surface=ctx.surface)
    if notes:
        text = text.rstrip("\n") + "\n" + "\n".join(f"  {n}" for n in notes) + "\n"
    return result("/stats", text, *keys, data=rep)
