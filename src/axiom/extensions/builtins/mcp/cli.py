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
from typing import Optional

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
    """List supported peer harness recipes (write support: Phase 2)."""
    if getattr(args, "write", False):
        print(
            "axi mcp clients --write: writes Tier-1 harness configs in Phase 2 "
            "(spec §9). See `axi mcp generate` for the legacy per-extension flow.",
            file=sys.stderr,
        )
    print("Supported MCP client harnesses:")
    for client in _SUPPORTED_CLIENTS:
        print(f"  - {client}")
    print()
    print("docs: docs/working/mcp-harness-adapters/<harness>.md")
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

    p_clients = sub.add_parser("clients", help="List supported peer harnesses.")
    p_clients.add_argument("--write", action="store_true", default=False)
    p_clients.add_argument("--harness", default="")

    sub.add_parser("generate", help="(Deprecated) alias for clients --write.")

    return parser


def main(argv: Optional[list[str]] = None) -> int:
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
    }
    handler = handlers.get(args.action)
    if handler is None:
        parser.print_help()
        return 1
    return handler(args)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())


__all__ = ["main"]
