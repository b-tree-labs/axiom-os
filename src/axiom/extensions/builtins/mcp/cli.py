# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""``axi mcp`` — manage the node-level root MCP server.

Spec: ``docs/specs/spec-builtin-mcp-server.md`` §9.

Subcommands:

- ``serve``       — run the stdio server in foreground (alias for
                    ``python -m axiom.extensions.builtins.mcp.server``).
- ``status``      — print cached surface summary (counts + content hash).
- ``list-tools``  — pretty-print the surface's tool list.
- ``inspect``     — show one tool's metadata (input schema, source).
- ``regenerate``  — force-rewalk extensions; rewrite ``surface.json``.
- ``clients``     — list supported peer harness recipes.
- ``generate``    — deprecated alias for ``clients --write``; staged
                    removal per spec §13.

The HTTP/SSE + token subcommands are spec'd as Phase 5; v1 stubs print
a clear "Phase 5" message rather than raising obscure errors.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from axiom.extensions.builtins.mcp.aggregation import (
    AggregationRegistry,
    MCPSurface,
)


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

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


_SUPPORTED_CLIENTS: tuple[str, ...] = (
    "claude_code",
    "cursor",
    "claude_desktop",
    "goose",
    "cline",
    "continue",
    "windsurf",
)


def _axiom_home() -> Path:
    """Resolve ``$AXIOM_HOME`` (or ``~/.axiom``) honouring the test sandbox."""
    env = os.environ.get("AXIOM_HOME")
    if env:
        return Path(env)
    return Path(os.environ.get("HOME", ".")).expanduser() / ".axiom"


def _surface_cache_path() -> Path:
    return _axiom_home() / "mcp" / "surface.json"


def _build_surface() -> MCPSurface:
    """Fresh surface from the live discovery walk."""
    return AggregationRegistry.from_node().build()


def _write_cache(surface: MCPSurface) -> Path:
    cache = _surface_cache_path()
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(
        json.dumps(surface.to_dict(), indent=2, default=str),
        encoding="utf-8",
    )
    return cache


def _load_or_build_surface() -> MCPSurface:
    """Phase 1: always rebuild. Phase 4 will load from cache when fresh."""
    return _build_surface()


# ---------------------------------------------------------------------------
# Subcommand handlers
# ---------------------------------------------------------------------------


def _cmd_serve(_args: argparse.Namespace) -> int:
    """Run the stdio server in the foreground."""
    if getattr(_args, "http", False):
        print(
            f"{_brand_cli()} mcp serve --http: HTTP/SSE transport ships in Phase 5 "
            "(see docs/adrs/adr-038-builtin-mcp-server.md D6).",
            file=sys.stderr,
        )
        return 2
    from axiom.extensions.builtins.mcp.server import main as server_main

    server_main()
    return 0


def _cmd_status(_args: argparse.Namespace) -> int:
    from axiom.infra.cli_format import (
        SQUARE,
        Column,
        Glyph,
        elide,
        relative_age,
        table,
        terminal_width,
    )

    surface = _load_or_build_surface()
    tools = len(surface.tools)
    width = terminal_width(reserve=2)

    verdict = Glyph.OK if tools else Glyph.WARN
    summary = (
        f"{tools} tool{'s' if tools != 1 else ''} "
        f"from {len(surface.sources)} contributors"
        if tools
        else "no tools published"
    )
    print(f"\n  MCP surface  {verdict}  {summary}")

    # The hash is sixty-four characters of something nobody reads in full;
    # what it is for is comparing two of them, which the head does. The
    # generated time is an age for the same reason as everywhere else.
    print()
    for line in table(
        [
            ("node", str(_axiom_home())),
            ("tools", str(tools)),
            ("resources", str(len(surface.resources))),
            ("prompts", str(len(surface.prompts))),
            ("generated", relative_age(surface.generated_at.isoformat())),
            ("hash", elide(surface.content_hash, 24)),
        ],
        [Column("mcp"), Column("value", wrap=True)],
        width=width,
        headers=True,
        border=SQUARE,
    ):
        print(line)

    if surface.sources:
        # "(8 entries)" told you a count and nothing else, so the next thing
        # anyone did was run list-tools and read all sixteen to find the two
        # they wanted. The names are the detail, so they go here.
        rows = []
        for src in surface.sources:
            names = list(src.tool_names) + list(src.resource_names) + list(src.prompt_names)
            # Strip the `axiom_` namespace and the contributor's own name: the
            # row already says who provides these, so repeating it inside every
            # entry only pushes the part that distinguishes them out of view.
            # `triga_telemetry__telemetry_metrics` was eliding to
            # `triga_telemetry_…emetry_metrics`, hiding the only useful half.
            trimmed = [
                n.removeprefix("axiom_").removeprefix(f"{src.name}__") for n in names
            ]
            rows.append((src.kind, src.name, str(len(names)), ", ".join(sorted(trimmed))))
        print()
        for line in table(
            rows,
            [
                Column("kind"),
                Column("contributor"),
                Column("entries", align="right"),
                Column("provides", wrap=True),
            ],
            width=width,
            headers=True,
            border=SQUARE,
        ):
            print(line)

    _print_harnesses(width=width)

    print()
    print(f"  {_brand_cli()} mcp list-tools --source <contributor>   ·   {_brand_cli()} mcp inspect <tool>")
    return 0


def _print_harnesses(*, width: int) -> None:
    """The half of status that is about the harnesses rather than the surface.

    Every question people arrived with during onboarding was about this half
    and none of it was printed: whether an editor is configured, which file
    says so, what it will launch, and why an upgrade changed nothing. The
    counts above describe what the server *would* publish; this describes what
    is set up to ask for it.

    One block per harness rather than a table. The two useful values are an
    absolute config path and an absolute command line, and three columns of
    those in an eighty-column terminal elides both — which loses precisely the
    path somebody has to open.
    """
    from axiom.infra.cli_format import Glyph, relative_age

    from axiom.extensions.builtins.mcp import harnesses, runs

    try:
        views = harnesses.survey()
        started = runs.read_runs()
        said = harnesses.complaints(views=views, started=started)
    except Exception as exc:  # noqa: BLE001 — status is what you run when broken
        print(f"\n  harnesses  {Glyph.WARN}  could not be read ({exc})")
        return

    if not views:
        print(f"\n  harnesses  {Glyph.WARN}  none configured")
        print()
        print(f"  Run `{_brand_cli()} mcp install` to register this server with an editor.")
        return

    verdict = Glyph.WARN if said else Glyph.OK
    print(f"\n  harnesses  {verdict}  {len(views)} configured")
    for v in views:
        print()
        print(f"  {v.tool}")
        print(f"    configured in   {v.config_path}")
        print(f"    runs            {v.runs_what}")

    # What each harness is actually serving, which stops being what is
    # installed the moment somebody upgrades without restarting — the single
    # most expensive surprise of the onboarding session.
    print()
    if started:
        for r in started:
            age = relative_age(r.started_at.isoformat())
            print(f"  serving         {r.harness}: {r.version}, started {age}")
    else:
        print("  No server has started from this install yet. One starts when a")
        print("  harness next launches, and this is where it will be reported.")

    for complaint in said:
        print()
        print(f"  {Glyph.WARN}  {complaint}")


def _cmd_list_tools(args: argparse.Namespace) -> int:
    surface = _load_or_build_surface()
    name_to_source = {}
    for src in surface.sources:
        for n in src.tool_names:
            # The source's own name, never its kind. Collapsing every
            # platform-kind source to the literal "platform" was harmless while
            # there was exactly one of them, and became wrong the moment a
            # second arrived: the prompt-access tools would have printed
            # `[platform]` and `--source prompts` would have selected nothing,
            # which is the same defect as `[?]` wearing a better label.
            name_to_source[n] = src.name

    wanted = getattr(args, "source", None)
    tools = surface.tools
    if wanted:
        tools = [t for t in tools if name_to_source.get(t.name) == wanted]
        if not tools:
            known = sorted(set(name_to_source.values()))
            print(
                f"{_brand_cli()} mcp list-tools: no contributor named {wanted!r}. "
                f"Known: {', '.join(known)}",
                file=sys.stderr,
            )
            return 1

    _print_tool_rows(tools, name_to_source)
    return 0


def _print_tool_rows(tools, name_to_source: dict) -> None:
    """One block per tool: its name and contributor, then its wrapped purpose.

    This was a single unwrapped line per tool — a name padded to forty columns
    whatever its length, then the whole description. Several descriptions on
    this surface run past 300 characters, deliberately, because they say what
    the tool is for. On an eighty-column console that is a wall, which is what a
    colleague met on 2026-10-01.

    A block rather than a table, for one reason: a tool name is something people
    copy, and a name wrapped across two lines cannot be copied. So the name
    stays whole on its own line and the description wraps beneath it, which also
    means the layout cannot run out of room and raise however narrow the window
    is.
    """
    import textwrap

    from axiom.infra.cli_format import terminal_width

    width = terminal_width(reserve=2)
    body = max(20, width - 4)
    for tool in tools:
        source = name_to_source.get(tool.name, "?")
        print(f"{tool.name}  [{source}]")
        description = (tool.description or "").strip()
        if description:
            for line in textwrap.wrap(description, width=body):
                print(f"    {line}")


def _cmd_inspect(args: argparse.Namespace) -> int:
    surface = _load_or_build_surface()
    target = args.tool
    matches = [t for t in surface.tools if t.name == target]
    if not matches:
        print(f"{_brand_cli()} mcp inspect: no such tool {target!r}", file=sys.stderr)
        return 1
    tool = matches[0]
    name_to_source = {}
    for src in surface.sources:
        for n in src.tool_names:
            # The source's own name, never its kind. Collapsing every
            # platform-kind source to the literal "platform" was harmless while
            # there was exactly one of them, and became wrong the moment a
            # second arrived: the prompt-access tools would have printed
            # `[platform]` and `--source prompts` would have selected nothing,
            # which is the same defect as `[?]` wearing a better label.
            name_to_source[n] = src.name
    print(f"name:        {tool.name}")
    print(f"description: {tool.description}")
    print(f"source:      {name_to_source.get(tool.name, '?')}")
    print("input_schema:")
    schema = getattr(tool, "input_schema", None) or {}
    print(json.dumps(schema, indent=2))
    return 0


def _cmd_regenerate(_args: argparse.Namespace) -> int:
    surface = _build_surface()
    cache = _write_cache(surface)
    print(f"{_brand_cli()} mcp: regenerated surface ({len(surface.tools)} tools) -> {cache}")
    return 0


def _cmd_clients(args: argparse.Namespace) -> int:
    """Chart of supported agent harnesses (clients) × EC-routing capability.

    Answers: which harnesses is it OK to route to an export-controlled model?
    """
    from .install import client_capabilities

    rows = client_capabilities()

    if getattr(args, "json", False):
        print(json.dumps(rows, indent=2))
        return 0

    print("Agent harnesses — OK to route to an export-controlled (EC) model?\n")
    header = f"  {'CLIENT':<16}{'PROTOCOL':<11}{'MCP TOOLS':<11}{'EC-ROUTABLE':<13}NOTES"
    print(header)
    print("  " + "-" * (len(header) - 2))
    for r in rows:
        ec = "✓ yes" if r["ec_routable"] else "✗ no"
        mcp = "✓"
        print(f"  {r['client']:<16}{r['protocol']:<11}{mcp:<11}{ec:<13}{r['notes']}")
    print(
        "\n  EC-routable = the harness's model can be put in-enclave (routed to the local\n"
        "  ingress). The MCP server WITHHOLDS export-controlled tool output from any client\n"
        "  that is not EC-routable. See docs/specs/spec-ec-client-capability.md."
    )
    return 0


def _cmd_generate(_args: argparse.Namespace) -> int:
    """Deprecated shim — tells the user to use ``axi mcp clients --write``."""
    print(
        f"{_brand_cli()} mcp generate is deprecated; use `{_brand_cli()} mcp clients --write` instead. "
        f"(Phase 1: legacy `{_brand_cli()} mcp generate` still routes to "
        "axiom.extensions.cli per back-compat plan §13.)",
        file=sys.stderr,
    )
    return 0


# ---------------------------------------------------------------------------
# Entry
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=f"{_brand_cli()} mcp",
        description="Manage the node-level root MCP server.",
    )
    sub = parser.add_subparsers(dest="action")

    p_serve = sub.add_parser("serve", help="Run the stdio MCP server.")
    p_serve.add_argument("--http", action="store_true", default=False)
    p_serve.add_argument("--port", type=int, default=0)
    p_serve.add_argument("--auth", default="local_stdio")

    sub.add_parser("status", help="Show surface summary.")
    p_tools = sub.add_parser("list-tools", help="List the surface's tools.")
    p_tools.add_argument(
        "--source",
        metavar="CONTRIBUTOR",
        help=f"Only tools from this contributor (see `{_brand_cli()} mcp status`).",
    )

    p_inspect = sub.add_parser("inspect", help="Inspect one tool.")
    p_inspect.add_argument("tool")

    sub.add_parser("regenerate", help="Force-rebuild + write the surface cache.")

    p_clients = sub.add_parser(
        "clients", help="Chart of agent harnesses × EC-routing capability."
    )
    p_clients.add_argument("--write", action="store_true", default=False)
    p_clients.add_argument("--harness", default="")
    p_clients.add_argument("--json", action="store_true", default=False,
                           help="Emit the capability matrix as JSON.")

    sub.add_parser("generate", help="(Deprecated) alias for clients --write.")

    p_install = sub.add_parser(
        "install", help="Register the unified MCP server into your IDE(s)/TUI(s)."
    )
    p_install.add_argument(
        "--tool", action="append",
        help="Target a specific client (repeatable). Default: all detected.",
    )
    p_install.add_argument(
        "--all", action="store_true", default=False,
        help="Write configs for every supported client, even if not detected.",
    )
    p_install.add_argument(
        "--dry-run", action="store_true", default=False,
        help="Show what would change without writing.",
    )
    p_install.add_argument(
        "--route-model", action="store_true", default=False,
        help="Also start the LLM ingress and repoint the client's MODEL at it "
             "(e.g. Claude Code's ANTHROPIC_BASE_URL). Off by default — this "
             "redirects which LLM the IDE talks to.",
    )

    p_uninstall = sub.add_parser(
        "uninstall", help="Remove the MCP server from your IDE(s)/TUI(s)."
    )
    p_uninstall.add_argument(
        "--tool", action="append",
        help="Target a specific client (repeatable). Default: all supported.",
    )
    p_uninstall.add_argument(
        "--dry-run", action="store_true", default=False,
        help="Show what would change without writing.",
    )

    return parser


def _cmd_install(args) -> int:
    from .install import install, supported_tools

    res = install(
        tools=args.tool or None, dry_run=bool(args.dry_run),
        all_tools=bool(args.all), route_model=bool(args.route_model),
    )
    head = "Would install" if res["dry_run"] else "Installed"
    print(f"{head} Axiom MCP server '{res['server']}'\n")

    if not res["results"]:
        print("No MCP-capable IDEs detected.")
        print("  Re-run with --all to write every supported config, or --tool <name>.")
        print("  Supported: " + ", ".join(supported_tools()))
        return 0

    # Per-client lines: action + EC status + any model wiring, aligned.
    print("  Clients:")
    for tool, r in res["results"].items():
        ec = r.get("ec_capable")
        if ec == "true":
            ectag = "EC-capable"
        elif ec == "false":
            ectag = "tools only (EC output withheld)"
        else:
            ectag = ""
        wires = []
        if r.get("base_url"):
            wires.append("model→ingress")
        if r.get("chat_models"):
            wires.append("Copilot BYOK")
        tail = f" — {ectag}" if ectag else ""
        tail += f" — {', '.join(wires)}" if wires else ""
        print(f"    • {tool:<14} {r['action']}{tail}")

    if res.get("route_model"):
        ing = res.get("ingress", {})
        print(f"\n  Ingress service: {ing.get('action', '?')} ({ing.get('provider', '')})")
        if not res["dry_run"]:
            _pick_default_provider()
        if "vscode" in res["results"]:
            print("\n  VS Code: restart, then pick 'Axiom (in-enclave)' in the chat model picker.")
            print("           For EC, disable Copilot Tab completions + embeddings (still GitHub-bound).")
    else:
        print("\n  Tools only. Add --route-model to also route the IDE's model through Axiom.")

    _print_restart_notice(sorted(res["results"]), dry_run=bool(res["dry_run"]))
    return 0


def _print_restart_notice(tools: list[str], *, dry_run: bool) -> None:
    """Say which harnesses to restart, and why.

    This used to be one generic line — "Restart the IDE (or reload its MCP
    config)" — at the end of a long output, and during onboarding on
    2026-10-01 nobody acted on it. Two things were wrong with it. It named no
    harness, so it read as boilerplate rather than as a step. And the "or
    reload its MCP config" alternative does not exist in most editors, so the
    one instruction that works was offered as the less convenient of two.

    A harness keeps the server process it already started. Saying so is what
    turns the restart from a ritual into a consequence, and it is the sentence
    whose absence cost a colleague an afternoon.
    """
    if dry_run:
        return
    named = ", ".join(tools)
    print()
    print(f"  Now restart {named}.")
    print("  Each one keeps the server process it already started, so a harness")
    print("  that is open right now goes on serving the previous install until it")
    print(f"  relaunches. `{_brand_cli()} mcp status` says which ones are behind.")


def _pick_default_provider() -> None:
    """Interactive: choose which provider the gateway routes to by default
    (sets routing.prefer_provider). Numbered list, arrow-navigable / type-number.
    No-ops cleanly when non-interactive or on any error."""
    try:
        from axiom.extensions.builtins.settings.store import SettingsStore
        from axiom.llm.gateway import Gateway

        from ._picker import select_index

        names = [p.name for p in Gateway().providers]
        if not names:
            return
        store = SettingsStore()
        current = store.get("routing.prefer_provider", [])
        cur_name = (current[0] if isinstance(current, list) and current else current) or ""
        default = names.index(cur_name) if cur_name in names else 0

        idx = select_index("\n  Default provider to route through Axiom:", names, default)
        if idx is None:
            print("  (kept current default)")
            return
        chosen = names[idx]
        store.set("routing.prefer_provider", [chosen])
        print(f"  Default provider → {chosen}")
    except Exception:  # noqa: BLE001 — picker is convenience, never fatal
        pass


def _cmd_uninstall(args) -> int:
    from .install import uninstall

    res = uninstall(tools=args.tool or None, dry_run=bool(args.dry_run))
    verb = "would remove" if res["dry_run"] else "removing"
    print(f"{verb} the MCP server from client config(s):")
    for tool, r in res["results"].items():
        print(f"  {tool:<16} {r['action']:<12} {r['config_path']}")
    print("\nRestart the IDE (or reload its MCP config) to apply.")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.action is None:
        parser.print_help()
        return 1

    handlers = {
        "serve": _cmd_serve,
        "status": _cmd_status,
        "list-tools": _cmd_list_tools,
        "inspect": _cmd_inspect,
        "regenerate": _cmd_regenerate,
        "clients": _cmd_clients,
        "generate": _cmd_generate,
        "install": _cmd_install,
        "uninstall": _cmd_uninstall,
    }
    handler = handlers.get(args.action)
    if handler is None:
        parser.print_help()
        return 1
    return handler(args)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())


__all__ = ["main"]
