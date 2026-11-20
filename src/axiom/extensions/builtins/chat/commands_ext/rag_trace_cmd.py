# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""`/rag on|off|shared` for everyone; `/trace on|off` for administrators only (ADR-168).

`/rag off` answers from the model alone for the rest of the session: nothing is retrieved, so nothing
grounds the answer, and the origin stamp says `RAG: OFF` so nobody mistakes an ungrounded answer for a
grounded one. `/rag shared` retrieves from the shared corpora only, as a surface answering other people
does: a terminal otherwise also searches this machine's own corpus, where its conversations are indexed,
so asking a question twice grounds the second answer in the first. The conversation is not indexed either,
so testing what the documents say leaves nothing behind for a later search to find. `/trace` controls the detailed per-turn trace shown under each reply. It does not touch the
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
    "shared": FollowUp("/rag shared", "search the shared documents only, not this machine's own conversations"),
}
TRACE_FOLLOW_UPS = {
    "stats": FollowUp("/stats", "per-turn latency, tokens and tool calls to read next to the trace"),
    "off": FollowUp("/trace off", "hide the trace"),
    "on": FollowUp("/trace on", "show the trace"),
}


@command(
    "/rag",
    "Turn retrieval on or off for this session (off = the model answers alone, shared = shared documents only)",
    "Model & routing",
    usage="/rag [on|off|shared|status]",
    follow_ups=RAG_FOLLOW_UPS,
    cli=CliSpec(("--rag",), line="/rag {values}", choices=("on", "off", "shared"),
                help="start with retrieval on, off, or from the shared documents only"),
    subcommands={
        "on": "Ground answers in retrieved documents",
        "off": "Answer from the model alone",
        "shared": "Ground answers in the shared documents only, and keep this conversation out of search",
        "status": "Show whether retrieval is on",
    },
)
def rag_command(ctx, args):
    session = ctx.agent.session
    action = args[0].lower() if args else "status"
    if action not in {"on", "off", "shared", "status"}:
        return "\n  Usage: /rag [on|off|shared|status]\n"
    if action in {"off", "shared"}:
        session.context["rag"] = action
    elif action == "on":
        session.context.pop("rag", None)
    off = _state(session, "rag", "off")
    shared = _state(session, "rag", "shared")
    if action == "status":
        state = "OFF" if off else "on, from the shared documents only" if shared else "on"
        return result("/rag", f"\n  Retrieval is {state} for this session.\n", "on" if off or shared else "off", "where")
    if off:
        return result(
            "/rag",
            "\n  Retrieval is OFF for this session. Answers come from the model alone and will not cite "
            "documents; replies are stamped RAG: OFF. Turn it back on with /rag on.\n",
            "on", "where",
        )
    if shared:
        return result(
            "/rag",
            "\n  Retrieval is on, from the shared documents only (rag-community, rag-org): not from this "
            "machine's own corpus, where its past conversations are indexed, and this conversation is not "
            "added to it. Search every corpus again with /rag on.\n",
            "sources", "on",
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
