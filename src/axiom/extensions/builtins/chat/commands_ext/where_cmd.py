# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""`/where`: where the next answer will come from, and where the last one did (ADR-168)."""

from __future__ import annotations

import json

from .. import where
from ..command_registry import CliSpec, McpForm, command
from ..follow_ups import FollowUp, pick, result, with_json_follow_ups

FOLLOW_UPS = {
    "providers": FollowUp("/model", "list the providers and where each one runs, or switch"),
    "sources": FollowUp("/sources", "what grounded the last reply, and where retrieval ran"),
    "rag_off": FollowUp("/rag off", "answer from the model alone if retrieval running elsewhere is not acceptable"),
    "planes": FollowUp("/planes", "everything this chat is connected to"),
}


def confirm_note(report) -> str:
    """How to turn a guessed or remote placement into a confirmed one, in plain words ('' when nothing to say)."""
    p = report.placement
    if not report.provider:
        return "No provider is selected, so there is no placement to confirm. /model lists what is configured."
    if not p.verified:
        return (
            f"The placement of {report.provider} is a guess from its address. To confirm it, set placement "
            "(local, cloud or remote-node) and node_host on its entry in llm-providers.toml."
        )
    if p.placement == "remote-node":
        return (
            f"{report.provider} runs on another machine ({p.host or 'host unknown'}). Content leaves this one; "
            "/model shows the providers that do not."
        )
    return ""


def follow_up_keys(report) -> list[str]:
    keys = ["providers", "sources"]
    if report.retrieval.kind == "remote-node":
        keys.append("rag_off")
    return keys


def where_data(agent) -> dict:
    """``/where --json``, ``axi chat --where --json`` and the ``axiom_chat__where`` tool all return this."""
    report = where.where_report(agent)
    out = with_json_follow_ups(where.to_dict(report), pick("/where", *follow_up_keys(report)))
    note = confirm_note(report)
    if note:
        out["confirm"] = note
    return out


@command(
    "/where",
    "Which model answers, where it runs (local, cloud, remote node), and where retrieval comes from",
    "Model & routing",
    usage="/where [--json]",
    follow_ups=FOLLOW_UPS,
    cli=CliSpec(("--where",), line="/where", applies="headless", json=True,
                help="print where the next answer comes from and exit"),
    expose=frozenset({"chat", "cli", "mcp"}),
    mcp=(McpForm("chat.where"),),
)
def where_command(ctx, args):
    report = where.where_report(ctx.agent)
    if "--json" in args:
        return json.dumps(where_data(ctx.agent), indent=2)
    text = "\n" + "\n".join("  " + line for line in where.render_block(report).splitlines()) + "\n"
    note = confirm_note(report)
    if note:
        text = text.rstrip("\n") + f"\n  {note}\n"
    return result("/where", text, *follow_up_keys(report))
