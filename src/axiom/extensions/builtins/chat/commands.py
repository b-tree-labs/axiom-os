# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Slash command implementations for neut chat.

Each command is a standalone function for testability.
Commands return a string to display, or None for no output.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from axiom.infra.text_utils import gutter, header, surface_block
from axiom.setup.renderer import _c, _Colors

from .planes import retrieval_summary
from .user_commands import load_user_commands


def _brand_cli() -> str:
    """The command the operator actually typed.

    These lines said "axi" unconditionally, so a consumer distribution's CLI
    told the operator to run a command that does not exist on their machine.
    """
    try:
        from axiom.infra.branding import get_branding

        return get_branding().cli_name
    except Exception:  # noqa: BLE001
        return "axi"

if TYPE_CHECKING:
    from axiom.infra.orchestrator.session import Session, SessionStore

    from .agent import ChatAgent


def cmd_help(agent: Any = None, surface: str = "repl") -> str:
    """The help screen, generated from the registry so it cannot disagree with dispatch or completion."""
    from .builtin_commands import ensure_loaded
    from .command_registry import REGISTRY

    ensure_loaded()
    listing = get_slash_commands(agent, surface)
    admin = _viewer_is_admin(agent, surface)
    lines = [gutter(header("Chat commands"))]
    for group in REGISTRY._group_order:
        members = [c for c in REGISTRY.commands(admin=admin) if c.group == group]
        if not members:
            continue
        lines.append("")
        lines.append(f"  {header(group)}")
        for c in members:
            label = _c(_Colors.CYAN, c.usage or c.name)
            pad = max(1, 24 - len(c.usage or c.name))
            lines.append(f"  {label}{' ' * pad}{c.summary}")
            # Subcommands: declared ones, plus those the CLI namespaces contribute (/signal brief, /pub status).
            for full, text in listing.items():
                if full.startswith(c.name + " "):
                    lines.append(f"  {_c(_Colors.CYAN, full)}{' ' * max(1, 24 - len(full))}{text}")

    # User-authored slash commands (~/.axi/commands/*.md + <project>/.axi/commands/*.md)
    user_cmds = load_user_commands()
    if user_cmds:
        lines.append("")
        lines.append(f"  {header('Your commands')}")
        for name, cmd in sorted(user_cmds.items()):
            lines.append(f"  {_c(_Colors.CYAN, name)}{' ' * max(1, 24 - len(name))}{cmd.description or '(no description)'}")

    lines.extend([
        "",
        f"  {_c(_Colors.DIM, 'Tip: Alt+Enter or Ctrl+J for newline · @path completes files')}",
        f"  {_c(_Colors.DIM, 'Author your own:')} {_c(_Colors.CYAN, '~/.axi/commands/<name>.md')} {_c(_Colors.DIM, 'or')} {_c(_Colors.CYAN, '<project>/.axi/commands/<name>.md')}",
    ])
    return surface_block(lines)


def _fmt_fields(pairs: list[tuple[str, str]]) -> list[str]:
    """Format (label, value) pairs with uniform label column width."""
    if not pairs:
        return []
    pad = max(len(label) for label, _ in pairs) + 2
    return [f"  {_c(_Colors.BOLD, label):{pad + len(_c(_Colors.BOLD, label)) - len(label)}}{value}" for label, value in pairs]


def cmd_status(agent: ChatAgent) -> str:
    """Return session status info with routing visibility."""
    session = agent.session
    gateway = agent.gateway
    provider = gateway.active_provider

    title_display = session.title or "(untitled)"
    pairs: list[tuple[str, str]] = [
        ("Session:", f"{session.session_id} — {title_display}"),
        ("Messages:", str(len(session.messages))),
    ]
    try:
        from . import where as _where

        _report = _where.where_report(agent)
        if _report.provider:
            pairs.append(("Runs:", f"{_report.placement.label()}  (model {_report.model}; retrieval {_where._retrieval_label(_report.retrieval)})"))
    except Exception:  # noqa: BLE001 - the placement line is additive; /status must still answer
        pass

    if provider:
        model_name = getattr(gateway, "_model_override", None) or provider.model
        tier = getattr(provider, "routing_tier", "any")
        tier_labels = {
            "public": "public (cloud)",
            "export_controlled": "export-controlled (VPN)",
            "any": "unrestricted",
        }
        pairs.append(("Provider:", provider.name))
        pairs.append(("Model:", model_name))
        pairs.append(("Tier:", tier_labels.get(tier, tier)))

        reasons = []
        if getattr(gateway, "_provider_override", None):
            reasons.append(f"user override (--provider {gateway._provider_override})")
        elif getattr(gateway, "_model_override", None):
            reasons.append(f"model override (--model {gateway._model_override})")
        else:
            reasons.append(f"priority {provider.priority}")
        if getattr(provider, "requires_vpn", False):
            vpn_ok = gateway._check_vpn(provider)
            reasons.append(f"VPN {'reachable' if vpn_ok else 'UNREACHABLE'}")
        session_mode = getattr(agent, "_session_mode", "auto")
        if session_mode != "auto":
            reasons.append(f"session mode: {session_mode}")
        pairs.append(("Reason:", "; ".join(reasons)))

        fallback_names = []
        for p in sorted(gateway.providers, key=lambda p: p.priority):
            if p.name != provider.name and (p.api_key or not p.api_key_env):
                vpn_tag = " [vpn]" if getattr(p, "requires_vpn", False) else ""
                fallback_names.append(f"{p.name} ({p.model}){vpn_tag}")
        pairs.append(("Fallback:", " -> ".join(fallback_names) if fallback_names else _c(_Colors.DIM, "none")))

        if agent.usage.turns:
            last = agent.usage.turns[-1]
            model_label = last.model or "unknown"
            pairs.append(("Last call:", f"{last.input_tokens + last.output_tokens} tokens, model={model_label}"))
    else:
        pairs.append(("Gateway:", _c(_Colors.DIM, "stub mode (no LLM configured)")))
    pairs.append(("Mode:", f"{getattr(agent, '_interaction_mode', 'agent')} (/mode)"))
    pairs.append(("Retrieval:", retrieval_summary()))

    usage = agent.usage
    if usage.turns:
        from axiom.infra.text_utils import pluralize
        pairs.append(("Tokens:", f"{usage.total_input_tokens:,} in / {usage.total_output_tokens:,} out ({pluralize(usage.turn_count, 'turn')})"))
        pairs.append(("Cost:", usage.cost_label()))

    if session.context:
        ctx_keys = list(session.context.keys())
        pairs.append(("Context:", str(ctx_keys)))

    pad = max(len(label) for label, _ in pairs) + 2
    lines = [gutter(header("Status"))]
    for label, value in pairs:
        lines.append(f"  {_c(_Colors.BOLD, label):<{pad + len(_c(_Colors.BOLD, label)) - len(label)}}{value}")
    return surface_block(lines)


def cmd_usage(agent: ChatAgent) -> str:
    """Return detailed token usage and cost breakdown."""
    usage = agent.usage
    if not usage.turns:
        return "\n  No usage data yet.\n"

    lines = [
        gutter(header("Tokens")),
        f"  Total input:  {usage.total_input_tokens:,}",
        f"  Total output: {usage.total_output_tokens:,}",
        f"  Total cost:   {usage.cost_label()}",
        f"  Model calls:  {usage.turn_count}",
        "",
        f"  {header('Per model call')}  (see /stats for per-turn)",
    ]
    # Columns rather than hand-set widths: a model name is occasionally long
    # enough to push the numbers off the edge, and `{model:30s}` cannot know
    # that. No border — this renders inside the prompt-toolkit TUI, which
    # treats a vertical as a column separator.
    from axiom.infra.cli_format import Column, table

    from .usage import format_cost

    rows = [
        (
            f"{i}.",
            turn.model or "unknown",
            f"{turn.input_tokens}in",
            f"{turn.output_tokens}out",
            format_cost(turn.cost),
        )
        for i, turn in enumerate(usage.turns, 1)
    ]
    if rows:
        lines.extend(
            table(
                rows,
                [
                    Column("#", align="right"),
                    Column("model", wrap=True),
                    Column("in", align="right"),
                    Column("out", align="right"),
                    Column("cost", align="right"),
                ],
                width=76,
            )
        )
    return surface_block(lines)


def cmd_tasks(args: list[str]) -> str:
    """Quick view of background tasks. Defers to `axi tasks list` for the
    full surface; this slash-command path keeps the chat session reading
    its own and peer tasks without context-switching."""
    from axiom.infra.tasks.runner import TaskRunner
    from axiom.infra.tasks.store import TaskStore

    store = TaskStore()
    runner = TaskRunner(store)
    status_filter = None
    if args and args[0] in ("running", "done", "failed", "cancelled", "pending"):
        status_filter = args[0]
    tasks = store.list(status=status_filter)
    if not tasks:
        return "\n  No tasks." if not status_filter else f"\n  No {status_filter} tasks.\n"
    # Refresh status for any 'running' tasks so the user sees the live state.
    refreshed = []
    for t in tasks:
        if t.status == "running":
            try:
                t = runner.status(t.task_id)
            except Exception:
                pass
        refreshed.append(t)

    lines = ["", f"  {_c(_Colors.BOLD, 'Background tasks:')}"]
    # `{coloured:20s}` pads by the width of the escape codes, so every column
    # after a coloured status walked off by a dozen invisible characters. The
    # table measures visible width instead.
    from axiom.infra.cli_format import Column, table

    rows = []
    for t in refreshed[:15]:
        status_color = {
            "running": _Colors.GREEN,
            "done": _Colors.DIM,
            "failed": _Colors.RED,
            "cancelled": _Colors.YELLOW,
            "pending": _Colors.CYAN,
        }.get(t.status, _Colors.DIM)
        rows.append(
            (
                _c(_Colors.CYAN, t.task_id),
                _c(status_color, t.status),
                (t.started_at or t.created_at or "")[:19],
                t.name,
            )
        )
    if rows:
        lines.extend(
            table(
                rows,
                [
                    Column("task"),
                    Column("status"),
                    Column("started"),
                    Column("name", wrap=True),
                ],
                width=76,
            )
        )
    if len(refreshed) > 15:
        lines.append(f"  {_c(_Colors.DIM, f'... and {len(refreshed) - 15} more')}")
    lines.append("")
    lines.append(
        f"  {_c(_Colors.DIM, f'Spawn from shell with: {_brand_cli()} tasks spawn <name> <command...>')}"
    )
    lines.append("")
    return "\n".join(lines)


def cmd_image(agent: ChatAgent, args: list[str]) -> str:
    """Queue an image to attach to the next user turn.

    Usage: /image <path>
    """
    from .attachments import ImageAttachment

    if not args:
        return (
            "\n  Usage: /image <path>\n"
            "  Attaches an image (png/jpg/gif/webp) to your next message.\n"
        )
    path = args[0]
    try:
        att = ImageAttachment.from_path(path)
    except FileNotFoundError:
        return f"\n  Image not found: {path}\n"
    except ValueError as exc:
        return f"\n  {exc}\n"

    pending = getattr(agent, "_pending_images", None)
    if pending is None:
        pending = []
        agent._pending_images = pending  # type: ignore[attr-defined]
    pending.append(att)
    return (
        f"\n  Attached: {_c(_Colors.CYAN, att.path.name)} ({att.media_type}, "
        f"queued for next turn — {len(pending)} pending)\n"
    )


def cmd_permissions(agent: ChatAgent, args: list[str] | None = None) -> str:
    """Show or modify per-tool permissions, and show what is held for a human.

    Usage:
      /permissions                         overrides, then the hold-for-human queue
      /permissions set <tool> <mode>       allow | ask | deny
      /permissions reset [<tool>]          clear one tool, or all if omitted
      /permissions held                    only the hold-for-human queue
    """
    from .commands_ext._held import format_held
    from .permissions import format_permissions

    args = args or []
    if not args:
        return format_permissions(agent.permissions).rstrip("\n") + "\n" + format_held() + "\n"

    sub = args[0].lower()
    if sub == "held":
        return "\n" + format_held() + "\n"
    if sub == "set" and len(args) >= 3:
        tool, mode = args[1], args[2].lower()
        try:
            agent.permissions.set(tool, mode)  # type: ignore[arg-type]
        except ValueError as exc:
            return f"\n  {_c(_Colors.RED, str(exc))}\n"
        return f"\n  Set {_c(_Colors.CYAN, tool)} -> {_c(_Colors.BOLD, mode)}\n"
    if sub == "reset":
        target = args[1] if len(args) > 1 else None
        agent.permissions.reset(target)
        scope = target or "all tools"
        return f"\n  Reset permissions for {_c(_Colors.CYAN, scope)}\n"
    return (
        "\n  Usage: /permissions [set <tool> <allow|ask|deny>] | [reset [<tool>]] | [held]\n"
    )


def cmd_planes() -> str:
    """Report what this chat is connected to, plane by plane (``/planes``).

    Domain (retrieval), person (identity), session (memory) and trace (logs):
    each local, served or unconfigured, with its secret-free source. Reads
    configuration only; never opens a network connection.
    """
    from .planes import format_plane_report, plane_report

    return format_plane_report(plane_report())


def cmd_advisor(args: list[str] | None = None) -> str:
    """Show or toggle the post-turn advisor hook.

    Usage:
      /advisor            status: enabled?, local-SLM contributor?, last tip
      /advisor on|off     persist chat.advisor.enabled (per-user)
      /advisor mute       never show the last tip again (decline memo)

    The advisor only OFFERS one dim line after a turn; it never runs anything.
    """
    from . import advisor as _adv

    args = args or []
    sub = args[0].lower() if args else "status"

    if sub in ("on", "off"):
        _adv.set_enabled(sub == "on")
        state = _c(_Colors.BOLD, sub)
        return f"\n  Advisor {state}. Tips are offered after a turn, never executed.\n"

    if sub == "mute":
        last = _adv.last_shown()
        if last is None:
            return "\n  No tip has been shown yet; nothing to mute.\n"
        _adv.record_decline(last.key)
        return f"\n  Muted {_c(_Colors.CYAN, last.key)}; that tip won't be offered again.\n"

    if sub == "status":
        try:
            from .slm_advisor import slm_enabled
            slm = "on" if slm_enabled() else "off"
        except Exception:
            slm = "off"
        enabled = "on" if _adv.is_enabled() else "off"
        last = _adv.last_shown()
        lines = [
            "",
            f"  Advisor: {_c(_Colors.BOLD, enabled)}  (chat.advisor.enabled)",
            f"  Local-SLM contributor: {_c(_Colors.BOLD, slm)}  (chat.advisor.slm)",
        ]
        if last is not None:
            lines.append(f"  Last tip [{last.source}]: {_c(_Colors.DIM, last.text)}")
        lines.append("  Tips are offered, never executed. /advisor on|off|mute")
        lines.append("")
        return "\n".join(lines)

    return "\n  Usage: /advisor [status|on|off|mute]\n"


def cmd_clear(agent: ChatAgent) -> str:
    """Clear message history, keep session open."""
    agent.session.messages.clear()
    return f"\n  {_c(_Colors.DIM, 'Message history cleared.')}\n"


def cmd_model(agent: ChatAgent) -> str:
    """List the providers with where each one runs, and whether this session's content may go to it."""
    from axiom.llm.placement import placement_of
    from axiom.llm.tier_policy import admits

    from . import where

    gateway = agent.gateway
    content_tier = where.content_tier_of(agent.session)
    try:
        answers_next = where.where_report(agent).provider
    except Exception:  # noqa: BLE001 - the marker is additive; the list must still render
        answers_next = getattr(getattr(gateway, "active_provider", None), "name", "")
    lines = [gutter(header("Providers"))]
    for i, p in enumerate(gateway.providers, 1):
        marker = "*" if p.name == answers_next else " "
        usable = bool(p.api_key or not p.api_key_env)
        notes = []
        if not usable:
            notes.append("no key")
        if not admits(p.routing_tier, content_tier):
            notes.append(f"not allowed: session holds {content_tier} content")
        note = f"  {_c(_Colors.DIM, '(' + '; '.join(notes) + ')')}" if notes else ""
        lines.append(f"  {marker} {i}. {p.name} ({p.model})  {placement_of(p).label()}  [{p.routing_tier}]{note}")
    lines.append(f"  {_c(_Colors.DIM, 'Switch with: /model <number|name>   (* = answers next)')}")
    return surface_block(lines)


def cmd_model_switch(agent: ChatAgent, provider_name: str) -> str:
    """Switch LLM provider mid-chat, refusing a provider the session's content may not go to (ADR-168)."""
    from axiom.llm.placement import placement_of
    from axiom.llm.tier_policy import ProviderNotAdmitted

    from . import where
    from .errors import friendly

    gateway = agent.gateway
    providers = list(gateway.providers)
    chosen = None
    if provider_name.isdigit():
        index = int(provider_name)
        if not 1 <= index <= len(providers):
            return f"\n  No provider numbered {index}: there are {len(providers)}. Run /model to list them.\n"
        chosen = providers[index - 1]
    else:
        chosen = next((p for p in providers if p.name.lower() == provider_name.lower()), None)
    if chosen is None:
        exc = ValueError(f"Provider not found: {provider_name}")
        return friendly(exc, providers=[p.name for p in providers])
    try:
        gateway.set_provider_override(chosen.name, content_tier=where.content_tier_of(agent.session))
    except ProviderNotAdmitted as refusal:
        return f"\n  {_c(_Colors.YELLOW, 'Not switched.')} {refusal}\n"
    return (
        f"\n  Switched to {_c(_Colors.CYAN, chosen.name)} ({chosen.model}), runs {placement_of(chosen).label()}\n"
    )


def cmd_context(agent: ChatAgent) -> str:
    """Show the conversation's size against the active model's window (estimates are labelled)."""
    from .stats import render_context

    return render_context(agent)


def cmd_doctor(agent: ChatAgent) -> str:
    """Quick health check: gateway, DB, extensions."""
    _DIM = _c(_Colors.DIM, "")[:4] if _c(_Colors.DIM, "") else ""  # noqa

    def _dim(s: str) -> str:
        return _c(_Colors.DIM, s)

    triples: list[tuple[str, str, str]] = []

    # Gateway
    gw = agent.gateway
    if gw.available:
        provider = gw.active_provider
        triples.append(("Gateway:", f"OK ({provider.name if provider else 'no provider'})", ""))
    else:
        triples.append(("Gateway:", _c(_Colors.RED, "UNAVAILABLE"), f"set ANTHROPIC_API_KEY or run `{_brand_cli()} config`"))

    # DB connectivity
    try:
        from axiom.infra.orchestrator.session import SessionStore

        s = SessionStore()
        count = len(s.list_sessions())
        triples.append(("Sessions:", f"OK ({count} saved)", ""))
    except Exception as e:
        triples.append(("Sessions:", _c(_Colors.RED, f"ERROR: {e}"), "check ~/.config/axiom/sessions/"))

    # Extensions
    try:
        from axiom.extensions.registry import get_registry

        reg = get_registry()
        ext_count = len(reg.extensions) if hasattr(reg, "extensions") else "?"
        triples.append(("Extensions:", f"{ext_count} loaded", ""))
    except Exception:
        triples.append(("Extensions:", _dim("unknown"), f"run `{_brand_cli()} ext list` to verify"))

    pad = max(len(label) for label, _, _ in triples) + 2
    lines = [gutter(header("Health"))]
    for label, status, hint in triples:
        hint_part = f"   {_dim(hint)}" if hint else ""
        lines.append(
            f"  {_c(_Colors.BOLD, label):<{pad + len(_c(_Colors.BOLD, label)) - len(label)}}{status}{hint_part}"
        )
    return surface_block(lines)


def cmd_signal() -> str:
    """Return signal pipeline status."""
    from .tools import execute_tool

    result = execute_tool("signal_status", {})
    lines = [gutter(header("Signal"))]
    inbox = result.get("inbox_raw", {})
    if inbox:
        for source, count in inbox.items():
            lines.append(f"  inbox/{source}: {count} files")
    else:
        lines.append("  inbox: empty")
    lines.append(f"  processed: {result.get('processed', 0)}")
    lines.append(f"  drafts: {result.get('drafts', 0)}")
    return surface_block(lines)


def cmd_doc() -> str:
    """Return document status."""
    from .tools import execute_tool

    result = execute_tool("query_docs", {})
    lines = [gutter(header("Documents"))]
    docs = result.get("documents", [])
    if not docs:
        lines.append("  No tracked documents.")
    else:
        for d in docs:
            status = d.get("status", "unknown")
            version = d.get("version", "")
            lines.append(f"  {d['doc_id']}: {status} ({version})")
    return surface_block(lines)


def _visible_ids(store: SessionStore, agent: ChatAgent | None) -> list[str]:
    """The session ids the current principal may see, in the order every numbered list uses. Without an
    agent (no one to scope to) it is the store's whole listing, as it always was."""
    session = getattr(agent, "session", None) if agent is not None else None
    if session is None:
        return store.list_sessions()
    from .commands_ext.renew import session_rows

    return [r["id"] for r in session_rows(store, session)]


def cmd_sessions(store: SessionStore, input_prov=None, agent: ChatAgent | None = None) -> str:
    """Return formatted list of sessions with titles.

    If input_prov supports interactive selection (PTK), offers arrow-key
    session picking. Otherwise falls back to a numbered list.
    """
    session_ids = _visible_ids(store, agent)
    if not session_ids:
        return "\n  No saved sessions.\n"

    # A session title is arbitrary user text and was the thing most likely to
    # run off the edge; it wraps in its own column now. No border — the
    # prompt-toolkit TUI treats a vertical as a column separator.
    from axiom.infra.cli_format import Column, table

    lines = [gutter(header("Saved sessions"))]
    rows = []
    for i, sid in enumerate(session_ids[:15], 1):
        meta = store.load_meta(sid)
        if meta:
            rows.append(
                (
                    _c(_Colors.DIM, f"{i}."),
                    _c(_Colors.CYAN, sid),
                    meta.get("title") or _c(_Colors.DIM, "(untitled)"),
                    _c(
                        _Colors.DIM,
                        f"{meta['message_count']} msgs  "
                        f"{meta['updated_at'][:10] if meta['updated_at'] else ''}",
                    ),
                )
            )
        else:
            rows.append((_c(_Colors.DIM, f"{i}."), _c(_Colors.CYAN, sid), "", ""))
    lines.extend(
        table(
            rows,
            [
                Column("#", align="right"),
                # Capped because their real content is bounded. Without a cap
                # a single unexpectedly long value in a non-wrapping column
                # takes the whole table down — the layout has no give and the
                # primitive correctly refuses rather than mangling. A cap
                # keeps one odd row from costing everyone the table.
                Column("id", max_width=24),
                Column("title", wrap=True),
                # Count and date ride together: five columns did not fit in a
                # 76-column pane and the table said so rather than mangling
                # them, which is the refusal doing its job.
                Column("when", max_width=22),
            ],
            width=76,
        )
    )
    if len(session_ids) > 15:
        lines.append(f"  {_c(_Colors.DIM, f'... and {len(session_ids) - 15} more')}")
    lines.extend(
        [
            "",
            f"  {_c(_Colors.DIM, 'Use')} {_c(_Colors.CYAN, '/renew <id>')} "
            f"{_c(_Colors.DIM, 'or')} {_c(_Colors.CYAN, '/renew <number>')} "
            f"{_c(_Colors.DIM, 'to load a session.')}",
        ]
    )
    return surface_block(lines)


def cmd_resume(
    session_id: str,
    store: SessionStore,
    agent: ChatAgent,
) -> str:
    """Resume a session by ID or number (from /sessions list)."""
    from .commands_ext.renew import resume_session, session_rows

    if session_id.isdigit():
        # A number from the /sessions list, which shows exactly what the asker may see.
        current = getattr(agent, "session", None)
        try:
            rows = session_rows(store, current)
        except Exception:  # noqa: BLE001 - a store that cannot be listed falls back to the id path
            rows = []
        idx = int(session_id) - 1
        if rows and 0 <= idx < len(rows):
            session_id = rows[idx]["id"]
        elif rows or len(session_id) < 4:
            return f"\n  {_c(_Colors.RED, 'Invalid session number:')} {session_id}\n"
    return resume_session(store, agent, session_id)


def cmd_new(store: SessionStore, agent: ChatAgent) -> str:
    """Start a fresh session. Returns status message."""
    # Save current session first
    store.save(agent.session)
    old_id = agent.session.session_id

    # Create new session
    new_session = store.create()
    agent.session = new_session

    return (
        f"\n  Saved {_c(_Colors.DIM, old_id)}, started {_c(_Colors.CYAN, new_session.session_id)}\n"
    )


def cmd_rename(agent: ChatAgent, store: SessionStore, title: str) -> str:
    """Rename the current session."""
    if not title:
        return "\n  Usage: /rename <title>\n"
    agent.session.title = title
    store.save(agent.session)
    return f"\n  Session renamed to: {_c(_Colors.CYAN, title)}\n"


def cmd_archive(
    session_id: str,
    store: SessionStore,
    agent: ChatAgent,
) -> str:
    """Archive a session (or the current one if no ID given)."""
    if not session_id:
        # Archive current session and start a new one
        target_id = agent.session.session_id
        store.save(agent.session)
        if store.archive(target_id):
            new_session = store.create()
            agent.session = new_session
            return (
                f"\n  Archived {_c(_Colors.DIM, target_id)}, "
                f"started {_c(_Colors.CYAN, new_session.session_id)}\n"
            )
        return f"\n  {_c(_Colors.RED, 'Failed to archive session')}\n"

    # Support numeric index
    if session_id.isdigit():
        idx = int(session_id) - 1
        all_ids = _visible_ids(store, agent)
        if 0 <= idx < len(all_ids):
            session_id = all_ids[idx]
        else:
            return f"\n  {_c(_Colors.RED, 'Invalid session number:')} {session_id}\n"

    if session_id == agent.session.session_id:
        return cmd_archive("", store, agent)

    if store.archive(session_id):
        return f"\n  Archived session {_c(_Colors.DIM, session_id)}\n"
    return f"\n  {_c(_Colors.RED, 'Session not found:')} {session_id}\n"


def cmd_save(session: Session, agent: ChatAgent | None = None, **kwargs) -> str:
    """Save the last assistant response as a knowledge fact in the local corpus."""
    if not session or not session.messages:
        return "\n  Nothing to save — no conversation yet.\n"

    # Find last assistant message
    last_assistant = None
    for msg in reversed(session.messages):
        if msg.role == "assistant":
            content = msg.content
            if isinstance(content, str) and content.strip():
                last_assistant = content
                break

    if not last_assistant:
        return "\n  No assistant response to save.\n"

    # Extract a summary (first paragraph or first 500 chars)
    summary = last_assistant.split("\n\n")[0][:500]

    # Save to knowledge events log
    try:
        from axiom.vega.federation.knowledge_metrics import KnowledgeMetricsService

        svc = KnowledgeMetricsService()
        session_id = getattr(session, "session_id", getattr(session, "id", "unknown"))
        svc.record_event(
            "fact_added",
            fact_id=f"chat-{str(session_id)[:8]}-{len(session.messages)}",
            source="chat_save",
            domain="user_knowledge",
            maturity=1,
            content=summary,
        )

        # Also save to personal RAG if available
        try:
            from axiom.rag.store import RAGStore

            store = RAGStore()
            store.add_chunk(
                text=last_assistant,
                source=f"chat:{session_id}",
                corpus="rag-internal",
                metadata={"type": "saved_fact", "session": str(session_id)},
            )
        except Exception:
            pass  # RAG store not available — that's OK

        truncated = summary[:80]
        return (
            f'\n  Saved to local knowledge corpus.\n  "{truncated}..."\n  Maturity: 1 (personal)\n'
        )
    except Exception as e:
        from .errors import friendly

        return friendly(e)


# ---------------------------------------------------------------------------
# Dispatch table (auto-synced from CLI registry)
# ---------------------------------------------------------------------------

class _RegistryMeta(Mapping):
    """``CHAT_META_COMMANDS``: the registry's completion metadata, read live.

    Kept as a mapping under the old name because completion, the unknown-command check and tests read it;
    it is no longer a table anyone edits. A command is declared once, in ``builtin_commands`` (or its own
    module), and this reflects it.
    """

    def _data(self) -> dict[str, str]:
        from .builtin_commands import ensure_loaded
        from .command_registry import REGISTRY

        ensure_loaded()
        return REGISTRY.meta()

    def __getitem__(self, key):
        return self._data()[key]

    def __iter__(self):
        return iter(self._data())

    def __len__(self):
        return len(self._data())

    def copy(self) -> dict[str, str]:
        return dict(self._data())


CHAT_META_COMMANDS = _RegistryMeta()


def _get_cli_commands() -> dict[str, str]:
    """Dynamically load CLI commands from registry.

    Returns commands keyed under their *visible* names (/signal, /pub).
    Hidden aliases (/sense, /doc) still work at dispatch time but are
    not included here so they stay out of /help and tab-completion.
    """
    cli_commands = {}

    # Import CLI modules and get their COMMANDS
    try:
        from axiom.extensions.builtins.signals.cli import COMMANDS as sense_commands

        for name, help_text in sense_commands.items():
            cli_commands[f"/signal {name}"] = help_text
    except ImportError:
        pass

    try:
        from axiom.extensions.builtins.publishing.cli import COMMANDS as doc_commands

        for name, help_text in doc_commands.items():
            cli_commands[f"/pub {name}"] = help_text
    except ImportError:
        pass

    return cli_commands


def _get_user_commands() -> dict[str, str]:
    """Load user-defined slash commands as a {/name: description} map."""
    try:
        from .user_commands import load_user_commands

        loaded = load_user_commands()
    except Exception:
        return {}
    return {f"/{name}": cmd.description for name, cmd in loaded.items()}


def _viewer_is_admin(agent: Any, surface: str) -> bool:
    if agent is None:
        return False
    from .roles import is_admin

    return is_admin(agent, surface=surface)


def get_slash_commands(agent: Any = None, surface: str = "repl") -> dict[str, str]:
    """Get all slash commands (meta + CLI + user-defined).

    This is the single source of truth for available slash commands.
    CLI commands are auto-synced from their respective modules.
    User-defined commands live in ``$AXI_STATE_DIR/commands/*.md`` and
    ``<project_root>/.<cli_name>/commands/*.md`` (project wins on collision).
    """
    from .builtin_commands import ensure_loaded
    from .command_registry import REGISTRY

    ensure_loaded()
    # What a viewer may see depends on who they are: an admin-only command is listed only for an admin.
    commands = REGISTRY.meta(admin=_viewer_is_admin(agent, surface))
    commands.update(_get_cli_commands())
    commands.update(_get_user_commands())
    return commands


def find_close_command(cmd: str) -> str | None:
    """Return the closest matching slash command, or None.

    Uses difflib fuzzy matching on the first word of each command.
    Shared by both the classic REPL and the fullscreen TUI.
    """
    from difflib import get_close_matches

    all_commands = list(get_slash_commands().keys())
    first_words = [c.split()[0] for c in all_commands]
    matches = get_close_matches(
        cmd.split()[0],
        first_words,
        n=1,
        cutoff=0.5,
    )
    return matches[0] if matches else None


# For backwards compatibility
SLASH_COMMANDS = CHAT_META_COMMANDS.copy()  # Static fallback


def cmd_update(args: list[str]) -> str:
    """``/update``: check for a newer version. Applying one is ``axi update --pull`` (or the TUI)."""
    sub = args[0].lower() if args else "check"
    if sub == "check":
        try:
            from axiom.extensions.builtins.update.version_check import VersionChecker

            info = VersionChecker().check_remote_version(timeout=10.0)
            if info.is_newer:
                return (
                    f"\n  Update available: {info.current} \u2192 {info.available}\n"
                    f"  Run '{_brand_cli()} update --pull' to update.\n"
                )
            return f"\n  Already up to date ({info.current}).\n"
        except Exception as e:  # noqa: BLE001
            return f"\n  Could not check: {e}\n"
    return f"\n  /update is fully supported in the fullscreen TUI.\n  Use '{_brand_cli()} update --pull' from the command line.\n"


def cmd_alerts(agent: ChatAgent, args: list[str]) -> str:
    """``/alerts`` lists what is waiting; ``/alerts <id>`` acknowledges one.

    The full-screen TUI opens a picker for the list; every other surface (the line REPL, headless) gets
    this text. It was advertised and completed in the REPL but only handled in the TUI, so typing it
    there said "Unknown command".
    """
    principal = getattr(agent.session, "principal_id", "") or ""
    if not principal:
        return "\n  This conversation has no owner, so it has no inbox.\n"
    try:
        from axiom.extensions.builtins.notifications.inbox import InboxQuery, list_unread
        from axiom.extensions.builtins.notifications.inbox_db import DatabaseInboxStore
    except Exception:  # noqa: BLE001
        return "\n  Alerts are unavailable on this install.\n"
    try:
        store = DatabaseInboxStore()
        if args:
            prefix = args[0]
            rows = store.query(InboxQuery(recipient=principal, limit=500))
            matches = [r for r in rows if r.id.startswith(prefix)]
            if not matches:
                return f"\n  No alert starting {prefix!r}. `/alerts` lists them.\n"
            if len(matches) > 1:
                return f"\n  {prefix!r} matches {len(matches)} alerts; use more characters.\n"
            row = matches[0]
            try:
                took = store.acknowledge(row_id=row.id, by=principal)
            except PermissionError as exc:
                return f"\n  {exc}\n"
            return f"\n  alert {'acknowledged' if took else 'already acknowledged'}: {row.id[:8]}\n"
        waiting = list_unread(store, recipient=principal)
    except Exception as exc:  # noqa: BLE001
        return f"\n  Could not read alerts: {type(exc).__name__}: {exc}\n"
    if not waiting:
        return "\n  No alerts waiting.\n"
    lines = ["", f"  {header('Alerts waiting')}"]
    for row in list(waiting)[:20]:
        summary = getattr(row, "summary", "") or getattr(row, "title", "") or ""
        lines.append(f"  {row.id[:8]}  {summary}".rstrip())
    lines.append(f"  {_c(_Colors.DIM, '/alerts <id> acknowledges one')}")
    return "\n".join(lines) + "\n"
