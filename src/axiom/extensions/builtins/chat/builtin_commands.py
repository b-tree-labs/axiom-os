# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""The chat meta commands, declared once in the registry (ADR-168).

Each entry wraps the handler that already existed; behaviour is unchanged by the move. What changed is
that dispatch, help and completion now derive from these declarations, so a command added here is
discoverable by construction. New commands live in their own modules and register the same way.
"""

from __future__ import annotations

from .command_registry import REGISTRY, CliSpec, Command, CommandContext
from .follow_ups import FollowUp

G_MOST = "Most-used"
G_INSPECT = "Inspect"
G_SESSION = "Session"
G_AGENT = "Agent"
G_KNOWLEDGE = "Memory & knowledge"
G_SYSTEM = "System"


def _reg(name, summary, group, handler, **fields):
    REGISTRY.register(Command(name, summary, group, handler, **fields))


# ---- Most-used ----------------------------------------------------------------------------------


def _help(ctx: CommandContext, args: list[str]):
    from .commands import cmd_help

    return cmd_help()


def _exit(ctx: CommandContext, args: list[str]):
    from axiom.setup.renderer import _c, _Colors

    print(f"  {_c(_Colors.DIM, 'Goodbye.')}")
    return "exit"


def _model(ctx: CommandContext, args: list[str]):
    from .commands import cmd_model, cmd_model_switch

    return cmd_model_switch(ctx.agent, args[0]) if args else cmd_model(ctx.agent)


def _clear(ctx, args):
    from .commands import cmd_clear

    return cmd_clear(ctx.agent)


def _image(ctx, args):
    from .commands import cmd_image

    return cmd_image(ctx.agent, args)


_reg("/help", "Show available commands", G_MOST, _help)
_reg(
    "/model", "List providers (where each runs) or switch; refuses a provider the session's content may not use",
    G_MOST, _model, usage="/model [number|name]",
    cli=CliSpec(("--model", "--provider"), existing=True, metavar="NAME", help="choose the model or provider for the session"),
)
_reg("/clear", "Clear message history", G_MOST, _clear)
_reg("/image", "Attach image to next message (png/jpg/gif/webp)", G_MOST, _image, usage="/image <path>")
_reg("/exit", "Save and exit", G_MOST, _exit, aliases=("/quit",))


# ---- Inspect ------------------------------------------------------------------------------------


def status_data(agent) -> dict:
    """``/status --json`` and ``axi chat --status --json``: the session, where it runs, its mode and usage."""
    from . import where as _where

    sess = agent.session
    usage = agent.usage
    return {
        "session": {
            "id": sess.session_id,
            "title": sess.title or "",
            "messages": len(sess.messages),
            "content_tier": _where.content_tier_of(sess),
        },
        "where": _where.to_dict(_where.where_report(agent)),
        "mode": getattr(agent, "_interaction_mode", "agent"),
        "usage": {
            "model_calls": usage.turn_count,
            "input_tokens": usage.total_input_tokens,
            "output_tokens": usage.total_output_tokens,
            "unpriced_turns": usage.unpriced_turns,
        },
    }


def _status(ctx, args):
    import json

    from .commands import cmd_status
    from .follow_ups import pick, result, with_json_follow_ups

    keys = ["where", "usage", "doctor"]
    if "--json" in args:
        return json.dumps(with_json_follow_ups(status_data(ctx.agent), pick("/status", *keys)), indent=2)
    return result("/status", cmd_status(ctx.agent), *keys)


def _usage(ctx, args):
    from .commands import cmd_usage
    from .commands_ext.stats import PRICE_NOTE, SLOW_TURN_MS  # noqa: F401
    from .follow_ups import result

    text = cmd_usage(ctx.agent)
    if not ctx.agent.usage.turns:
        return text
    keys = ["stats", "context"]
    if ctx.agent.usage.unpriced_turns:
        text = text.rstrip("\n") + f"\n  {PRICE_NOTE}\n"
        keys.insert(0, "price")
    return result("/usage", text, *keys)


def _context(ctx, args):
    from .commands import cmd_context

    return cmd_context(ctx.agent)


def _doctor(ctx, args):
    from .commands import cmd_doctor

    return cmd_doctor(ctx.agent)


def _planes(ctx, args):
    from .commands import cmd_planes

    return cmd_planes()


_reg(
    "/status", "Session info, gateway, usage", G_INSPECT, _status, usage="/status [--json]",
    follow_ups={
        "where": FollowUp("/where", "where the next answer comes from"),
        "usage": FollowUp("/usage", "token and cost for each model call"),
        "doctor": FollowUp("/doctor", "a quick health check of the providers and services"),
    },
    cli=CliSpec(("--status",), line="/status", applies="headless", json=True,
                help="print the session, where it runs and its usage, and exit"),
)
_reg(
    "/usage", "Token usage and cost breakdown", G_INSPECT, _usage,
    follow_ups={
        "price": FollowUp("/where", "name the provider and model that went unpriced, then price that entry"),
        "stats": FollowUp("/stats", "the same totals per turn, with latency and tool calls"),
        "context": FollowUp("/context", "how full the context window is"),
    },
)
_reg("/context", "Show context window usage", G_INSPECT, _context)
_reg("/doctor", "Quick health check", G_INSPECT, _doctor)
_reg("/planes", "What this chat is connected to (retrieval, identity, memory, trace)", G_INSPECT, _planes)


# ---- Session ------------------------------------------------------------------------------------


def _sessions(ctx, args):
    from .commands import cmd_archive, cmd_rename, cmd_sessions

    if not args:
        return cmd_sessions(ctx.store, input_prov=None, agent=ctx.agent)
    sub = args[0].lower()
    if sub == "rename":
        return cmd_rename(ctx.agent, ctx.store, " ".join(args[1:]).strip())
    if sub == "archive":
        return cmd_archive(args[1].strip() if len(args) > 1 else "", ctx.store, ctx.agent)
    return f"\n  Unknown: /sessions {sub}\n"


def _resume(ctx, args):
    # The same handler as /renew: one picker, one resolution rule.
    from .commands_ext.renew import renew_handler

    return renew_handler(ctx, args)


def _complete_sessions(agent, args):
    from .commands_ext.renew import complete

    return complete(agent, args)


def _new(ctx, args):
    from .commands import cmd_new

    return cmd_new(ctx.store, ctx.agent)


def _rename(ctx, args):
    from .commands import cmd_rename

    return cmd_rename(ctx.agent, ctx.store, " ".join(args).strip())


def _archive(ctx, args):
    from .commands import cmd_archive

    return cmd_archive(args[0].strip() if args else "", ctx.store, ctx.agent)


def _save(ctx, args):
    from .commands import cmd_save

    return cmd_save(ctx.agent.session, agent=ctx.agent)


_reg(
    "/sessions", "Browse and manage sessions", G_SESSION, _sessions,
    subcommands={
        "rename": "Rename current session (/sessions rename <title>)",
        "archive": "Archive session(s) (/sessions archive [id|#])",
    },
)
_reg(
    "/resume", "Same as /renew: pick a previous session to resume", G_SESSION, _resume,
    usage="/resume [--all] [number|id|text]", complete=_complete_sessions,
)
_reg("/new", "Start a fresh session", G_SESSION, _new)
_reg("/save", "Save last assistant response to knowledge corpus", G_SESSION, _save)
# Legacy spellings of /sessions rename and /sessions archive: kept working, deliberately not advertised.
_reg("/rename", "Rename current session", G_SESSION, _rename, hidden=True)
_reg("/archive", "Archive session(s)", G_SESSION, _archive, hidden=True)


# ---- Agent --------------------------------------------------------------------------------------


def _permissions(ctx, args):
    from .commands import cmd_permissions

    return cmd_permissions(ctx.agent, args)


def _advisor(ctx, args):
    from .commands import cmd_advisor

    return cmd_advisor(args)


def _tasks(ctx, args):
    from .commands import cmd_tasks

    return cmd_tasks(args)


def _alerts(ctx, args):
    from .commands import cmd_alerts

    return cmd_alerts(ctx.agent, args)


_reg(
    "/permissions",
    "Tool permissions and the hold-for-human queue (set <tool> allow|ask|deny, reset [tool], held)",
    G_AGENT, _permissions,
    usage="/permissions [verb]",
    subcommands={
        "set": "Set a tool's mode (/permissions set <tool> allow|ask|deny)",
        "reset": "Clear one tool's override, or all (/permissions reset [tool])",
        "held": "Actions held for a human, with ids and how to approve",
    },
)
_reg("/advisor", "Post-turn tips that offer, never execute (/advisor [status|on|off|mute])", G_AGENT, _advisor)
_reg("/tasks", "List background tasks (optional status filter)", G_AGENT, _tasks)
_reg("/alerts", "Alerts waiting; acknowledge one (/alerts <id>)", G_AGENT, _alerts, usage="/alerts [id]")


# ---- System -------------------------------------------------------------------------------------


def _update(ctx, args):
    from .commands import cmd_update

    return cmd_update(args)


_reg("/update", "Check for and apply updates (/update [now|later|check])", G_SYSTEM, _update)


# ---- Memory & knowledge (forwarded to the CLI namespaces) ---------------------------------------


def _forward(ctx, args):
    # Re-assembled from the original line by the dispatcher in cli.py, which owns the namespace mapping.
    raise RuntimeError("forwarded by the dispatcher")  # pragma: no cover


# /signal and /pub are dispatched to the CLI namespaces; they are declared here so help and completion see them.
_reg("/signal", "Signals: catch up, pipeline health (/signal brief | status)", G_KNOWLEDGE, _forward,
     aliases=("/sense",))
_reg("/pub", "Documents: status, overview (/pub status | overview)", G_KNOWLEDGE, _forward,
     aliases=("/doc", "/publisher"))


def ensure_loaded() -> None:
    """Declare the built-ins (importing this module does) and every module in ``commands_ext``."""
    from .commands_ext import load_all

    load_all()
    from .completers import wire_completers

    wire_completers(REGISTRY)
