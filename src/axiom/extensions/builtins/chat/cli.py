# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""CLI handler for `axi chat` — interactive agent with streaming.

Usage:
    axi chat                         Start a new chat session
    axi chat --resume <id>           Resume an existing session
    axi chat --context <file>        Load additional context from file
    axi chat --no-stream             Disable streaming output
    axi chat --model <name>          Override LLM model
    axi chat --provider <name>       Override LLM provider
    axi chat --render ansi|rich      Force render provider
    axi chat --input basic|ptk       Force input provider

The REPL reads user input, passes it through the ChatAgent
(which handles native tool calling and approval gates), and streams responses.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from axiom.infra.bus import EventBus
from axiom.infra.gateway import Gateway
from axiom.infra.orchestrator.session import Session, SessionStore
from axiom.setup.renderer import _c, _Colors

from .agent import ChatAgent
from .commands import (
    cmd_archive,
    cmd_clear,
    cmd_compact,
    cmd_context,
    cmd_doc,
    cmd_doctor,
    cmd_help,
    cmd_model,
    cmd_model_switch,
    cmd_new,
    cmd_permissions,
    cmd_rename,
    cmd_resume,
    cmd_save,
    cmd_sessions,
    cmd_signal,
    cmd_status,
    cmd_usage,
    find_close_command,
    get_slash_commands,
)
from .provider_factory import create_input_provider, create_render_provider
from .providers.base import InputProvider, RenderProvider
from .workspace import detect_workspace_context


def maybe_start_local_llm(gateway: Gateway) -> bool:
    """If a llamafile is already provisioned but not running, start it.

    This makes second-run-and-later `chat` "just work" without the user
    having to re-run setup — the first interactive setup downloads the
    llamafile + model, and subsequent invocations auto-start the binary
    if nothing else is configured.

    First run (nothing provisioned) is left alone: surprise-downloading
    a multi-GB model on a CLI invocation that the user might not expect
    to take minutes would be hostile. The fallback hint surfaced by
    `build_setup_hint` invites the user to opt in via `{cli} config`.

    Returns True if it transitioned the gateway from unavailable to
    available, False otherwise. Caller may use this to decide whether
    to skip the no-provider hint path.
    """
    if gateway.available:
        return False
    try:
        from axiom.setup import llamafile
    except ImportError:
        return False
    try:
        if not llamafile.is_llamafile_installed():
            return False
        if llamafile.is_llamafile_running():
            # Already running — re-trigger gateway discovery so it picks
            # up the localhost endpoint, then report status.
            gateway._discover_local_llm()  # noqa: SLF001 — public-enough
            return gateway.available
        # Provisioned but not running — start it.
        if not llamafile.start_llamafile():
            return False
        # Re-run the gateway's local-LLM probe so it sees the new endpoint.
        gateway._discover_local_llm()  # noqa: SLF001 — public-enough
    except Exception:
        # Best-effort: any failure in this auto-start path must fall
        # through to the fallback hint, never crash the chat startup.
        return False
    return gateway.available


def build_setup_hint(gateway: Gateway) -> str | None:
    """Return a setup hint string when no provider is usable, else None.

    A provider is usable if it has an api_key OR needs no key (empty api_key_env).
    """
    if gateway.available:
        return None

    from axiom.infra.branding import get_branding

    cli_name = get_branding().cli_name

    providers = getattr(gateway, "providers", [])
    missing_env_vars = [
        p.api_key_env
        for p in providers
        if p.api_key_env and not p.api_key
    ]

    lines = ["", "  No LLM provider is reachable.", ""]
    if missing_env_vars:
        lines.append("  Set the following environment variable(s) to get started:")
        for var in missing_env_vars:
            lines.append(f"    export {var}=<your-key>")
        lines.append("")
        lines.append(f"  Or run `{cli_name} config` to configure interactively.")
    else:
        # Free + local options first (no key, no separate install — the
        # `{cli} config` wizard provisions a llamafile binary + model).
        # Cloud options second for users who already have an API key.
        lines.append("  To get started, choose one of:")
        lines.append(
            f"    {cli_name} config                       # interactive setup (recommended)"
        )
        lines.append(
            f"    {cli_name} config --model bonsai        # local LLM (~1.7GB, no key, fastest)"
        )
        lines.append(
            f"    {cli_name} config --model qwen          # local LLM (~5GB, no key, better)"
        )
        lines.append(
            "    export ANTHROPIC_API_KEY=<your-key>     # Anthropic Claude (cloud)"
        )
        lines.append(
            "    export OPENAI_API_KEY=<your-key>        # OpenAI GPT (cloud)"
        )

    lines.append("")
    return "\n".join(lines)


def _print_model_status(gateway: Gateway | None = None) -> None:
    """Backward-compat stub — functionality moved to welcome banner."""
    pass


def run_repl(
    agent: ChatAgent,
    store: SessionStore,
    stream: bool = True,
    render: RenderProvider | None = None,
    input_prov: InputProvider | None = None,
    show_banner: bool = False,
) -> None:
    """Run the interactive REPL loop."""
    # Initialize providers
    if render is None:
        render = create_render_provider()
    if input_prov is None:
        input_prov = create_input_provider()

    # Wire render provider into agent
    agent.set_render_provider(render)

    # Set up input provider with slash command completions and dynamic arg-completers
    slash_cmds = get_slash_commands()  # dict[str, str]
    try:
        input_prov.setup(
            slash_commands=slash_cmds,
            providers_fn=lambda: [p.name for p in agent.gateway.providers],
            sessions_fn=lambda: list(store.list_sessions()),
        )
    except TypeError:
        # Legacy BasicInputProvider only accepts list
        input_prov.setup(slash_commands=list(slash_cmds.keys()))

    # Detect workspace context (model.yaml) and inject into agent
    workspace_ctx = detect_workspace_context()
    if workspace_ctx:
        agent._workspace_context = workspace_ctx

    render.render_welcome(
        gateway=agent.gateway,
        show_banner=show_banner,
        workspace_context=workspace_ctx,
    )

    # Chat-time federation re-probe nudge: one-line tip if a richer
    # remote LLM is reachable but the operator hasn't adopted it.
    # Cheap, TTY-only, swallows every exception — chat startup
    # MUST NOT block on this.
    try:
        from .federation_nudge import maybe_render_federation_nudge
        maybe_render_federation_nudge()
    except Exception:
        pass

    # Show resume info if session has history
    if agent.session.messages:
        title = agent.session.title or agent.session.session_id
        msg_count = len(agent.session.messages)
        print(f"  {_c(_Colors.DIM, f'Resuming: {title} ({msg_count} messages)')}")

    try:
        while True:
            try:
                print()  # Single blank line before prompt

                user_input = input_prov.prompt("> ", show_border=False)
            except KeyboardInterrupt:
                print()  # New prompt on Ctrl+C
                continue
            except EOFError:
                print(f"\n  {_c(_Colors.DIM, 'Goodbye.')}")
                break

            user_input = user_input.strip()
            if not user_input:
                continue

            # Support """ wrapping as alternative multiline delimiter
            if user_input.startswith('"""') and user_input.endswith('"""') and len(user_input) > 6:
                user_input = user_input[3:-3].strip()
                if not user_input:
                    continue

            # --- Bang-escape: !cmd runs shell command ---
            if user_input.startswith("!"):
                _run_bash_escape(user_input[1:].strip())
                continue

            # --- Slash commands ---
            if user_input.startswith("/"):
                from .user_commands import UserCommandPrompt

                handled = _handle_slash_command(user_input, agent, store)
                if handled == "exit":
                    break
                if isinstance(handled, UserCommandPrompt):
                    # User-defined command — fall through to the agent
                    # turn pipeline with the rendered prompt.
                    user_input = handled.prompt
                else:
                    if handled:
                        print(handled)
                    continue

            # Legacy exit commands
            if user_input.lower() in ("exit", "quit"):
                print(f"  {_c(_Colors.DIM, 'Goodbye.')}")
                break

            # --- Agent turn ---
            try:
                if stream and agent.gateway.available:
                    print()  # Blank line before response
                    response = agent.turn(user_input, stream=True)
                    print()  # Blank line after response
                else:
                    from .renderer import render_thinking_spinner

                    with render_thinking_spinner("Thinking"):
                        response = agent.turn(user_input, stream=False)
                    render.render_message("assistant", response)

                # Show status line after each turn
                model = ""
                if agent.gateway.active_provider:
                    model = agent.gateway.active_provider.model
                usage = agent.usage
                if usage.turns:
                    last = usage.turns[-1]
                    render.render_status(
                        model=model,
                        tokens_in=last.input_tokens,
                        tokens_out=last.output_tokens,
                        cost=last.cost,
                    )
            except KeyboardInterrupt:
                print(f"\n  {_c(_Colors.DIM, '[interrupted]')}")
                continue

            # Heartbeat for background subscribers (mirror, tidy, etc.)
            try:
                agent.bus.publish("tidy.heartbeat", {}, source="chat.repl")
            except Exception:
                pass

            # Auto-save after each turn
            store.save(agent.session)

            # Drain tab-queued slash commands from input provider
            if hasattr(input_prov, "drain_queue"):
                for queued_cmd in input_prov.drain_queue():
                    handled = _handle_slash_command(queued_cmd, agent, store)
                    if handled == "exit":
                        break
                    if handled:
                        print(handled)
    finally:
        input_prov.teardown()


def _handle_slash_command(
    command: str,
    agent: ChatAgent,
    store: SessionStore,
):
    """Dispatch a slash command.

    Returns one of:
      - ``"exit"`` (string sentinel) — break the chat loop
      - a ``str`` — text to print to the user
      - a ``UserCommandPrompt`` — caller routes ``.prompt`` through ``agent.turn()``
      - ``None`` — nothing to print

    Handles chat meta commands, CLI commands (auto-synced), and user-defined
    commands (``~/.axi/commands/*.md`` and ``<project>/.axi/commands/*.md``).
    """
    parts = command.split()
    cmd = parts[0].lower()

    # --- Chat meta commands ---
    if cmd in ("/exit", "/quit"):
        print(f"  {_c(_Colors.DIM, 'Goodbye.')}")
        return "exit"

    if cmd == "/help":
        return cmd_help()

    if cmd == "/status":
        return cmd_status(agent)

    if cmd == "/sessions":
        if len(parts) == 1:
            return cmd_sessions(store, input_prov=None)
        subcmd = parts[1].lower()
        if subcmd == "rename":
            title = " ".join(parts[2:]).strip() if len(parts) > 2 else ""
            return cmd_rename(agent, store, title)
        if subcmd == "archive":
            arg = parts[2].strip() if len(parts) > 2 else ""
            return cmd_archive(arg, store, agent)
        return f"\n  Unknown: /sessions {subcmd}\n"

    if cmd == "/resume":
        arg = parts[1].strip() if len(parts) > 1 else ""
        if not arg:
            return "\n  Usage: /resume <session_id>\n"
        return cmd_resume(arg, store, agent)

    if cmd == "/new":
        return cmd_new(store, agent)

    if cmd == "/rename":
        title = " ".join(parts[1:]).strip() if len(parts) > 1 else ""
        return cmd_rename(agent, store, title)

    if cmd == "/archive":
        arg = parts[1].strip() if len(parts) > 1 else ""
        return cmd_archive(arg, store, agent)

    if cmd == "/usage":
        return cmd_usage(agent)

    if cmd == "/clear":
        return cmd_clear(agent)

    if cmd == "/compact":
        return cmd_compact(agent)

    if cmd == "/model":
        if len(parts) > 1:
            return cmd_model_switch(agent, parts[1])
        return cmd_model(agent)

    if cmd == "/context":
        return cmd_context(agent)

    if cmd == "/permissions":
        args = parts[1:] if len(parts) > 1 else []
        return cmd_permissions(agent, args)

    if cmd == "/save":
        return cmd_save(agent.session, agent=agent)

    if cmd == "/tasks":
        from .commands import cmd_tasks

        return cmd_tasks(parts[1:])

    if cmd == "/image":
        from .commands import cmd_image

        return cmd_image(agent, parts[1:])

    if cmd == "/doctor":
        return cmd_doctor(agent)

    if cmd == "/update":
        subcmd = parts[1].lower() if len(parts) > 1 else "check"
        if subcmd == "check":
            try:
                from axiom.extensions.builtins.update.version_check import VersionChecker

                checker = VersionChecker()
                info = checker.check_remote_version(timeout=10.0)
                if info.is_newer:
                    return (
                        f"\n  Update available: {info.current} \u2192 {info.available}\n"
                        f"  Run 'axi update --pull' to update.\n"
                    )
                return f"\n  Already up to date ({info.current}).\n"
            except Exception as e:
                return f"\n  Could not check: {e}\n"
        return "\n  /update is fully supported in the fullscreen TUI.\n  Use 'axi update --pull' from the command line.\n"

    # --- CLI commands (forwarded to actual CLI) ---
    # /signal and /pub are the primary names; /sense and /doc are hidden aliases
    if cmd in ("/signal", "/sense", "/pub", "/doc", "/publisher"):
        return _execute_cli_command(command)

    # --- User-defined commands (~/.axi/commands/*.md or project .axi/commands) ---
    from .user_commands import try_dispatch_user_command

    user_match = try_dispatch_user_command(command)
    if user_match is not None:
        return user_match

    # --- Unknown command with suggestion ---
    suggestion = find_close_command(command)
    if suggestion:
        return f"\n  Unknown command: {cmd}. Did you mean {suggestion}?\n"
    return f"\n  Unknown command: {cmd}. Type /help for available commands.\n"


def _execute_cli_command(command: str) -> str:
    """Execute a CLI command via the registry.

    Supports full CLI syntax: /sense ingest --source voice
    """
    from axiom.cli_registry import execute_command

    parts = command.lstrip("/").split()
    if not parts:
        return "\n  No command specified.\n"

    namespace = parts[0]
    subcommand = parts[1] if len(parts) > 1 else ""
    args = parts[2:] if len(parts) > 2 else []

    # Map aliases to internal registry namespaces
    if namespace in ("signal", "sense"):
        namespace = "sense"
    elif namespace in ("pub", "doc", "publisher"):
        namespace = "doc"

    if not subcommand:
        # Show namespace status/help
        if namespace == "sense":
            return cmd_signal()
        elif namespace == "doc":
            return cmd_doc()
        return f"\n  Usage: /{namespace} <subcommand>\n"

    # Execute via registry
    result = execute_command(namespace, subcommand, args, capture_output=True)

    if result["success"]:
        output = result.get("output", "").strip()
        if output:
            return f"\n{output}\n"
        return f"\n  v /{namespace} {subcommand} completed\n"
    else:
        error = result.get("error", "Unknown error")
        return f"\n  x Error: {error}\n"


def get_parser() -> argparse.ArgumentParser:
    """Build and return the argument parser.

    Exposed for CLI registry introspection and argcomplete.
    """
    parser = argparse.ArgumentParser(
        prog="axi chat",
        description="Interactive agent with tool calling",
    )
    parser.add_argument(
        "--resume",
        nargs="?",
        const="__pick__",          # `--resume` with no id → open picker
        default=None,                # default (no flag) → fresh session
        metavar="SESSION_ID",
        help="Resume a chat session.  `--resume` (no id) opens the picker; "
             "`--resume <id>` resumes a specific session.  Default: start fresh.",
    )
    parser.add_argument(
        "--pick",
        action="store_true",
        help="Open the session picker (alias for bare `--resume`).",
    )
    parser.add_argument(
        "--context",
        metavar="FILE",
        help="Load additional context from a file",
    )
    parser.add_argument(
        "--no-stream",
        action="store_true",
        help="Disable streaming output",
    )
    parser.add_argument(
        "--model",
        metavar="NAME",
        help="Override LLM model for this session",
    )
    parser.add_argument(
        "--provider",
        metavar="NAME",
        help="Override LLM provider for this session",
    )
    parser.add_argument(
        "--mode",
        choices=["auto", "public", "export-controlled"],
        default="auto",
        help="Routing mode: auto (classify each query), public (cloud only), "
        "export-controlled (VPN model for all queries)",
    )
    parser.add_argument(
        "--render",
        choices=["rich", "ansi"],
        help="Force render provider (default: auto-detect)",
    )
    parser.add_argument(
        "--input",
        choices=["ptk", "basic"],
        dest="input_mode",
        help="Force input provider (default: auto-detect)",
    )
    parser.add_argument(
        "--bare",
        action="store_true",
        help=argparse.SUPPRESS,  # Internal: show full mascot banner
    )
    parser.add_argument(
        "--no-tui",
        action="store_true",
        help="Disable full-screen TUI, use classic REPL",
    )
    return parser


def _run_bash_escape(command: str) -> None:
    """Run a shell command from !cmd escape, print output."""
    import subprocess

    print(f"  {_c(_Colors.DIM, f'$ {command}')}")
    try:
        subprocess.run(command, shell=True, timeout=60)
    except subprocess.TimeoutExpired:
        print(f"  {_c(_Colors.RED, '[timed out after 60s]')}")


def _is_fullscreen_available() -> bool:
    """Check if full-screen TUI can launch (TTY + prompt_toolkit)."""
    if not hasattr(sys.stdin, "isatty") or not sys.stdin.isatty():
        return False
    if not hasattr(sys.stdout, "isatty") or not sys.stdout.isatty():
        return False
    try:
        import prompt_toolkit  # noqa: F401

        return True
    except ImportError:
        return False


def _check_restart_state() -> dict | None:
    """Check for restart state from a recent /update restart."""
    try:
        from axiom.extensions.builtins.update.version_check import read_restart_state

        return read_restart_state(max_age_seconds=60.0)
    except Exception:
        return None


def _clear_restart_state() -> None:
    """Clean up restart state file."""
    try:
        from axiom.extensions.builtins.update.version_check import clear_restart_state

        clear_restart_state()
    except Exception:
        pass


def main():
    parser = get_parser()
    args = parser.parse_args()

    # Auto-resume from restart state (e.g. after /update)
    restart_ctx: dict | None = None
    if not args.resume:
        restart_state = _check_restart_state()
        if restart_state:
            args.resume = restart_state["session_id"]
            restart_ctx = restart_state
            _clear_restart_state()

    store = SessionStore()
    gateway = Gateway()

    # Wire --provider and --model overrides (flags are parsed; now implemented)
    if getattr(args, "provider", None):
        gateway.set_provider_override(args.provider)
    if getattr(args, "model", None):
        gateway.set_model_override(args.model)

    bus = EventBus()

    # Wire background subscribers (soft — no-ops if extensions unavailable)
    try:
        from axiom.extensions.builtins.mirror_agent.subscriber import register as _mirror_reg

        _mirror_reg(bus)
    except ImportError:
        pass

    # Resume or create session
    session: Session | None = None
    if args.resume:
        session = store.load(args.resume)
        if session is None:
            print(f"Session '{args.resume}' not found.")
            sys.exit(1)
    else:
        context = {}
        if args.context:
            ctx_path = Path(args.context)
            if ctx_path.exists():
                context["loaded_file"] = str(ctx_path)
                context["file_content"] = ctx_path.read_text(encoding="utf-8")[:4000]
            else:
                print(f"Context file not found: {args.context}")
                sys.exit(1)
        session = store.create(context=context)

    stream = not args.no_stream

    # Create agent without a render provider — each branch wires its own
    agent = ChatAgent(
        gateway=gateway,
        bus=bus,
        session=session,
        render=None,
    )
    # Wire routing mode (--mode flag normalised: "export-controlled" → "export_controlled")
    if getattr(args, "mode", "auto") != "auto":
        agent._session_mode = args.mode.replace("-", "_")

    # Wire long-term memory (no-op when no node identity exists).
    try:
        from .memory_wiring import attach_memory

        attach_memory(agent)
    except Exception:
        # Best-effort — never block chat startup on a memory wiring failure.
        pass

    # Detect workspace context and inject into agent
    workspace_ctx = detect_workspace_context()
    if workspace_ctx:
        agent._workspace_context = workspace_ctx

    # If a llamafile is already provisioned but not running, auto-start
    # it so the user does not have to re-run setup each session. First-
    # run (nothing provisioned) is intentionally left to the fallback
    # hint — see maybe_start_local_llm for why.
    maybe_start_local_llm(gateway)

    # No-provider guard: show setup hint and exit if no LLM usable.
    if (
        hasattr(sys.stdin, "isatty") and sys.stdin.isatty()
        and hasattr(sys.stdout, "isatty") and sys.stdout.isatty()
    ):
        hint = build_setup_hint(gateway)
        if hint is not None:
            print(hint)
            sys.exit(0)

    # Default: start a fresh session (like `claude` does).  Open the
    # session picker only when the user explicitly asks via `--pick` or
    # bare `--resume` (the latter sets resume="__pick__").  Specifying
    # `--resume <id>` resumes that particular session and skips the
    # picker.  Behavior change 2026-05-04 — prior default was
    # auto-picker which surprised first-time users.
    auto_pick = (
        getattr(args, "pick", False)
        or args.resume == "__pick__"
    )
    # Treat `--resume __pick__` as "open picker, no specific session"
    if args.resume == "__pick__":
        args.resume = None

    # Try full-screen TUI first
    if not args.no_tui and _is_fullscreen_available():
        try:
            from .fullscreen import FullScreenChat

            tui = FullScreenChat(
                agent,
                store,
                stream=stream,
                show_banner=args.bare,
                restart_ctx=restart_ctx,
                auto_picker=auto_pick,
            )
            try:
                tui.run()
            finally:
                store.save(agent.session)
            return
        except Exception as _tui_err:
            import traceback as _tb

            print(f"[TUI failed, falling back to REPL: {_tui_err}]", file=sys.stderr)
            _tb.print_exc(file=sys.stderr)

    # Classic REPL fallback
    render = create_render_provider(force=args.render)
    input_prov = create_input_provider(force=args.input_mode)

    try:
        run_repl(
            agent, store, stream=stream, render=render, input_prov=input_prov, show_banner=args.bare
        )
    finally:
        store.save(agent.session)


if __name__ == "__main__":
    main()
