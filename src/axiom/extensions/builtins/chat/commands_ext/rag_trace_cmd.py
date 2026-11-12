# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""`/rag on|off` for everyone; `/trace on|off` for administrators only (ADR-168).

`/rag off` answers from the model alone for the rest of the session: nothing is retrieved, so nothing
grounds the answer, and the origin stamp says `RAG: OFF` so nobody mistakes an ungrounded answer for a
grounded one. `/trace` controls the detailed per-turn trace shown under each reply. It does not touch the
routing audit log: that record is a control, not a diagnostic, and a command in the chat cannot switch it off.
"""

from __future__ import annotations

from ..command_registry import CliSpec, command
from ..follow_ups import FollowUp, result


def _state(session, key, off_value):
    return session.context.get(key) == off_value


RAG_FOLLOW_UPS = {
    "where": FollowUp("/where", "see where the answer will come from now"),
    "sources": FollowUp("/sources", "what grounded the last reply"),
    "on": FollowUp("/rag on", "ground answers in retrieved documents again"),
    "off": FollowUp("/rag off", "answer from the model alone"),
}
TRACE_FOLLOW_UPS = {
    "stats": FollowUp("/stats", "per-turn latency, tokens and tool calls to read next to the trace"),
    "off": FollowUp("/trace off", "hide the trace"),
    "on": FollowUp("/trace on", "show the trace"),
}


@command(
    "/rag",
    "Turn retrieval on or off for this session (off = the model answers alone)",
    "Model & routing",
    usage="/rag [on|off|status]",
    follow_ups=RAG_FOLLOW_UPS,
    cli=CliSpec(("--rag",), line="/rag {values}", choices=("on", "off"),
                help="start with retrieval on or off"),
    subcommands={
        "on": "Ground answers in retrieved documents",
        "off": "Answer from the model alone",
        "status": "Show whether retrieval is on",
    },
)
def rag_command(ctx, args):
    session = ctx.agent.session
    action = args[0].lower() if args else "status"
    if action not in {"on", "off", "status"}:
        return "\n  Usage: /rag [on|off|status]\n"
    if action == "off":
        session.context["rag"] = "off"
    elif action == "on":
        session.context.pop("rag", None)
    off = _state(session, "rag", "off")
    if action == "status":
        return result("/rag", f"\n  Retrieval is {'OFF' if off else 'on'} for this session.\n", "on" if off else "off", "where")
    if off:
        return result(
            "/rag",
            "\n  Retrieval is OFF for this session. Answers come from the model alone and will not cite "
            "documents; replies are stamped RAG: OFF. Turn it back on with /rag on.\n",
            "on", "where",
        )
    return result(
        "/rag",
        "\n  Retrieval is on: answers are grounded in retrieved documents when any are relevant.\n",
        "where", "sources",
    )


@command(
    "/trace",
    "Show or hide the detailed per-turn trace (administrators only)",
    "System",
    usage="/trace [on|off|status]",
    follow_ups=TRACE_FOLLOW_UPS,
    cli=CliSpec(("--trace",), line="/trace on", help="start with the per-turn trace shown (administrators)"),
    subcommands={
        "on": "Show a trace under each reply",
        "off": "Hide the trace",
        "status": "Show whether the trace is on",
    },
    requires="admin",
)
def trace_command(ctx, args):
    session = ctx.agent.session
    action = args[0].lower() if args else "status"
    if action not in {"on", "off", "status"}:
        return "\n  Usage: /trace [on|off|status]\n"
    if action in {"on", "off"}:
        session.context["trace"] = action == "on"
    on = bool(session.context.get("trace"))
    if action == "status":
        return result("/trace", f"\n  Trace is {'on' if on else 'off'} for this session.\n", "off" if on else "on", "stats")
    note = (
        ""
        if on
        else " The routing audit log is not affected: it records every routing decision either way."
    )
    return result("/trace", f"\n  Trace {'on' if on else 'off'}.{note}\n", "off" if on else "on", "stats")
