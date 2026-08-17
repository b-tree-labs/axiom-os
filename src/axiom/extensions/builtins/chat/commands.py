# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Slash command implementations for neut chat.

Each command is a standalone function for testability.
Commands return a string to display, or None for no output.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from axiom.infra.text_utils import gutter, header, surface_block
from axiom.setup.renderer import _c, _Colors

from .user_commands import load_user_commands

if TYPE_CHECKING:
    from axiom.infra.orchestrator.session import Session, SessionStore

    from .agent import ChatAgent


def cmd_help() -> str:
    """Return the help text in 2-column grouped layout, with user commands appended."""
    import re as _re

    _ansi_strip = _re.compile(r"\x1b\[[0-9;]*m")

    def _strip(s: str) -> str:
        return _ansi_strip.sub("", s)

    def _col(cmd: str, desc: str, width: int = 22) -> str:
        cmd_str = _c(_Colors.CYAN, cmd)
        pad = max(1, width - len(_strip(cmd_str)))
        return f"  {cmd_str}{' ' * pad}{desc}"

    # Left column: most-used + diagnostics
    left = [
        f"  {header('Most-used')}",
        _col("/help", "Show this help"),
        _col("/model", "Switch LLM provider mid-chat"),
        _col("/clear", "Clear message history"),
        _col("/image <path>", "Attach image to next message"),
        _col("/exit", "Save and exit"),
        "",
        f"  {header('Diagnostics')}",
        _col("/status", "Session info, gateway, usage"),
        _col("/usage", "Token usage and cost breakdown"),
        _col("/context", "Show context window usage"),
        _col("/doctor", "Quick health check"),
        _col("/compact", "Summarize conversation to save tokens"),
        _col("/update", "Check for and apply updates"),
    ]

    # Right column: sessions + knowledge
    right = [
        f"  {header('Sessions')}",
        _col("/sessions", "Browse and manage sessions"),
        _col("/sessions rename", "Rename current session"),
        _col("/sessions archive", "Archive session(s)"),
        _col("/resume <id|#>", "Load a session by ID or number"),
        _col("/new", "Start a fresh session"),
        _col("/save", "Save last response to knowledge corpus"),
        _col("/permissions", "View/revoke always-allowlisted tools"),
        "",
        f"  {header('Knowledge')}",
        _col("/signal brief", "Catch up on what happened"),
        _col("/signal status", "Pipeline health"),
        _col("/pub status", "Document status"),
        _col("/pub overview", "Document ecosystem dashboard"),
    ]

    lines = [gutter(header("Chat commands"))]
    max_rows = max(len(left), len(right))
    for i in range(max_rows):
        lc = left[i] if i < len(left) else ""
        r = right[i] if i < len(right) else ""
        # Pad left to 40 chars (ANSI-aware)
        stripped_l = _strip(lc)
        pad = max(0, 40 - len(stripped_l))
        lines.append(f"{lc}{' ' * pad}{r}")

    # User-authored slash commands (~/.axi/commands/*.md + <project>/.axi/commands/*.md)
    user_cmds = load_user_commands()
    if user_cmds:
        lines.append("")
        lines.append(f"  {header('Your commands')}")
        for name, cmd in sorted(user_cmds.items()):
            lines.append(_col(name, cmd.description or "(no description)"))

    lines.extend([
        "",
        f"  {_c(_Colors.DIM, 'Tip: Alt+Enter or Ctrl+J for newline · !cmd runs shell · @path completes files')}",
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

    usage = agent.usage
    if usage.turns:
        from axiom.infra.text_utils import pluralize
        pairs.append(("Tokens:", f"{usage.total_input_tokens:,} in / {usage.total_output_tokens:,} out ({pluralize(usage.turn_count, 'turn')})"))
        if usage.total_cost > 0:
            pairs.append(("Cost:", f"${usage.total_cost:.4f}"))

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
        f"  Total cost:   ${usage.total_cost:.4f}",
        f"  Turns:        {usage.turn_count}",
        "",
        f"  {header('Per turn')}",
    ]
    for i, turn in enumerate(usage.turns, 1):
        model_label = turn.model or "unknown"
        cost_label = f"${turn.cost:.4f}" if turn.cost > 0 else "-"
        lines.append(
            f"  {i:3d}. {model_label:30s}  "
            f"{turn.input_tokens:>6d}in  {turn.output_tokens:>6d}out  {cost_label}"
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
    for t in refreshed[:15]:
        started = (t.started_at or t.created_at or "")[:19]
        status_color = {
            "running": _Colors.GREEN,
            "done": _Colors.DIM,
            "failed": _Colors.RED,
            "cancelled": _Colors.YELLOW,
            "pending": _Colors.CYAN,
        }.get(t.status, _Colors.DIM)
        lines.append(
            f"  {_c(_Colors.CYAN, t.task_id)}  "
            f"{_c(status_color, t.status):20s}  "
            f"{started}  "
            f"{t.name}"
        )
    if len(refreshed) > 15:
        lines.append(f"  {_c(_Colors.DIM, f'... and {len(refreshed) - 15} more')}")
    lines.append("")
    lines.append(
        f"  {_c(_Colors.DIM, 'Spawn from shell with: axi tasks spawn <name> <command...>')}"
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
    """Show or modify per-tool permissions.

    Usage:
      /permissions                         show current overrides
      /permissions set <tool> <mode>       allow | ask | deny
      /permissions reset [<tool>]          clear one tool, or all if omitted
    """
    from .permissions import format_permissions

    args = args or []
    if not args:
        return format_permissions(agent.permissions)

    sub = args[0].lower()
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
        "\n  Usage: /permissions [set <tool> <allow|ask|deny>] | [reset [<tool>]]\n"
    )


def cmd_clear(agent: ChatAgent) -> str:
    """Clear message history, keep session open."""
    agent.session.messages.clear()
    return f"\n  {_c(_Colors.DIM, 'Message history cleared.')}\n"


def cmd_compact(agent: ChatAgent) -> str:
    """Summarize conversation to save tokens (stub)."""
    return f"\n  {_c(_Colors.DIM, 'Compact not yet implemented — use /new to start fresh.')}\n"


def cmd_model(agent: ChatAgent) -> str:
    """Show current model and available providers."""
    gateway = agent.gateway
    lines = [gutter(header("Available providers"))]
    for p in gateway.providers:
        marker = " *" if p == gateway.active_provider else ""
        lines.append(f"  {p.name} ({p.model}){marker}")
    lines.append(f"  {_c(_Colors.DIM, 'Switch with: /model <provider-name>')}")
    return surface_block(lines)


def cmd_model_switch(agent: ChatAgent, provider_name: str) -> str:
    """Switch LLM provider mid-chat."""
    from .errors import friendly

    gateway = agent.gateway
    for p in gateway.providers:
        if p.name.lower() == provider_name.lower():
            gateway.set_provider_override(p.name)
            return f"\n  Switched to {_c(_Colors.CYAN, p.name)} ({p.model})\n"
    exc = ValueError(f"Provider not found: {provider_name}")
    providers = [p.name for p in gateway.providers]
    return friendly(exc, providers=providers)


def cmd_context(agent: ChatAgent) -> str:
    """Show context window usage."""
    from axiom.infra.text_utils import bar

    messages = agent.session.messages
    # Rough estimate: ~4 chars per token
    total_chars = sum(len(getattr(m, "content", "") or "") for m in messages)
    est_tokens = total_chars // 4
    # Most models use ~128k-200k context; show against 128k as baseline
    budget = 128_000
    pct = min(100.0, (est_tokens / budget) * 100) if budget else 0
    fill_bar = bar(pct, width=20)

    pairs = [
        ("Messages:", str(len(messages))),
        ("Est. tokens:", f"~{est_tokens:,}"),
        ("Budget:", f"{budget:,}"),
        ("Usage:", f"{fill_bar} {pct:.1f}%"),
    ]
    pad = max(len(label) for label, _ in pairs) + 2
    lines = [gutter(header("Context"))]
    for label, value in pairs:
        lines.append(f"  {_c(_Colors.BOLD, label):<{pad + len(_c(_Colors.BOLD, label)) - len(label)}}{value}")
    return surface_block(lines)


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
        triples.append(("Gateway:", _c(_Colors.RED, "UNAVAILABLE"), "set ANTHROPIC_API_KEY or run `axi config`"))

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
        triples.append(("Extensions:", _dim("unknown"), "run `axi ext list` to verify"))

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


def cmd_sessions(store: SessionStore, input_prov=None) -> str:
    """Return formatted list of sessions with titles.

    If input_prov supports interactive selection (PTK), offers arrow-key
    session picking. Otherwise falls back to a numbered list.
    """
    session_ids = store.list_sessions()
    if not session_ids:
        return "\n  No saved sessions.\n"

    lines = [gutter(header("Saved sessions"))]
    for i, sid in enumerate(session_ids[:15], 1):
        meta = store.load_meta(sid)
        if meta:
            title = meta.get("title") or _c(_Colors.DIM, "(untitled)")
            msg_count = meta["message_count"]
            updated = meta["updated_at"][:10] if meta["updated_at"] else ""
            idx = _c(_Colors.DIM, f"{i:2d}.")
            lines.append(
                f"  {idx} {_c(_Colors.CYAN, sid)}  "
                f"{title}  "
                f"{_c(_Colors.DIM, f'{msg_count} msgs  {updated}')}"
            )
        else:
            lines.append(f"  {_c(_Colors.DIM, f'{i:2d}.')} {_c(_Colors.CYAN, sid)}")
    if len(session_ids) > 15:
        lines.append(f"  {_c(_Colors.DIM, f'... and {len(session_ids) - 15} more')}")
    lines.extend(
        [
            "",
            f"  {_c(_Colors.DIM, 'Use')} {_c(_Colors.CYAN, '/resume <id>')} "
            f"{_c(_Colors.DIM, 'or')} {_c(_Colors.CYAN, '/resume <number>')} "
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
    # Support numeric index from /sessions list
    if session_id.isdigit():
        idx = int(session_id) - 1
        all_ids = store.list_sessions()
        if 0 <= idx < len(all_ids):
            session_id = all_ids[idx]
        else:
            return f"\n  {_c(_Colors.RED, 'Invalid session number:')} {session_id}\n"

    session = store.load(session_id)
    if session is None:
        return f"\n  {_c(_Colors.RED, 'Session not found:')} {session_id}\n"

    agent.session = session
    title_str = f" — {session.title}" if session.title else ""
    return (
        f"\n  Resumed session {_c(_Colors.CYAN, session_id)}{title_str} "
        f"({len(session.messages)} messages)\n"
    )


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
        all_ids = store.list_sessions()
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

# Chat-specific meta commands
CHAT_META_COMMANDS = {
    "/help": "Show available commands",
    "/status": "Session info, gateway, usage",
    "/usage": "Token usage and cost breakdown",
    "/sessions": "Browse and manage sessions",
    "/sessions rename": "Rename current session (/sessions rename <title>)",
    "/sessions archive": "Archive session(s) (/sessions archive [id|#])",
    "/resume": "Load a session by ID or number (/resume <id|#>)",
    "/new": "Start a fresh session",
    "/clear": "Clear message history",
    "/compact": "Summarize conversation to save tokens",
    "/model": "Switch LLM provider mid-chat",
    "/context": "Show context window usage",
    "/permissions": "View/revoke always-allowlisted tools (/permissions revoke <name>)",
    "/save": "Save last assistant response to knowledge corpus",
    "/tasks": "List background tasks (optional status filter)",
    "/image": "Attach image to next message (png/jpg/gif/webp)",
    "/doctor": "Quick health check",
    "/update": "Check for and apply updates (/update [now|later|check])",
    "/exit": "Save and exit",
}


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


def get_slash_commands() -> dict[str, str]:
    """Get all slash commands (meta + CLI + user-defined).

    This is the single source of truth for available slash commands.
    CLI commands are auto-synced from their respective modules.
    User-defined commands live in ``$AXI_STATE_DIR/commands/*.md`` and
    ``<project_root>/.<cli_name>/commands/*.md`` (project wins on collision).
    """
    commands = CHAT_META_COMMANDS.copy()
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
