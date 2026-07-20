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
            "axi mcp serve --http: HTTP/SSE transport ships in Phase 5 "
            "(see docs/adrs/adr-038-builtin-mcp-server.md D6).",
            file=sys.stderr,
        )
        return 2
    from axiom.extensions.builtins.mcp.server import main as server_main

    server_main()
    return 0


def _cmd_status(_args: argparse.Namespace) -> int:
    surface = _load_or_build_surface()
    print(f"node:        {_axiom_home()}")
    print(f"tools:       {len(surface.tools)}")
    print(f"resources:   {len(surface.resources)}")
    print(f"prompts:     {len(surface.prompts)}")
    print(f"hash:        {surface.content_hash}")
    print(f"generated:   {surface.generated_at.isoformat()}")
    print(f"contributors: {len(surface.sources)}")
    for src in surface.sources:
        n = (
            len(src.tool_names) + len(src.resource_names) + len(src.prompt_names)
        )
        print(f"  - {src.kind:<9} {src.name:<24} ({n} entries)")
    return 0


def _cmd_list_tools(_args: argparse.Namespace) -> int:
    surface = _load_or_build_surface()
    name_to_source = {}
    for src in surface.sources:
        for n in src.tool_names:
            name_to_source[n] = src.name if src.kind == "extension" else "platform"
    for tool in surface.tools:
        source = name_to_source.get(tool.name, "?")
        print(f"{tool.name:<40}  [{source}]  {tool.description or ''}")
    return 0


def _cmd_inspect(args: argparse.Namespace) -> int:
    surface = _load_or_build_surface()
    target = args.tool
    matches = [t for t in surface.tools if t.name == target]
    if not matches:
        print(f"axi mcp inspect: no such tool {target!r}", file=sys.stderr)
        return 1
    tool = matches[0]
    name_to_source = {}
    for src in surface.sources:
        for n in src.tool_names:
            name_to_source[n] = src.name if src.kind == "extension" else "platform"
    print(f"name:        {tool.name}")
    print(f"description: {tool.description}")
    print(f"source:      {name_to_source.get(tool.name, '?')}")
    print("input_schema:")
    schema = getattr(tool, "inputSchema", None) or {}
    print(json.dumps(schema, indent=2))
    return 0


def _cmd_regenerate(_args: argparse.Namespace) -> int:
    surface = _build_surface()
    cache = _write_cache(surface)
    print(f"axi mcp: regenerated surface ({len(surface.tools)} tools) -> {cache}")
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
        "axi mcp generate is deprecated; use `axi mcp clients --write` instead. "
        "(Phase 1: legacy `axi mcp generate` still routes to "
        "axiom.extensions.cli per back-compat plan §13.)",
        file=sys.stderr,
    )
    return 0


# ---------------------------------------------------------------------------
# Entry
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="axi mcp",
        description="Manage the node-level root MCP server.",
    )
    sub = parser.add_subparsers(dest="action")

    p_serve = sub.add_parser("serve", help="Run the stdio MCP server.")
    p_serve.add_argument("--http", action="store_true", default=False)
    p_serve.add_argument("--port", type=int, default=0)
    p_serve.add_argument("--auth", default="local_stdio")

    sub.add_parser("status", help="Show surface summary.")
    sub.add_parser("list-tools", help="List the surface's tools.")

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

    print("\n  Restart the IDE (or reload its MCP config) to pick up the server.")
    return 0


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
