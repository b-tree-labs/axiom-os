# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""CLI entry point for `axi ext`.

AEOS-aligned verbs (Tier 1/Tier 2 per spec-aeos-0.1 §10) go through the
Provider framework under :mod:`axiom.cli.ext` so third parties can override
each verb via the ``axiom.ext.cli.providers`` entry-point group:

  axi ext                    List installed extensions
  axi ext init <name>        Scaffold a new AEOS-conformant extension
  axi ext lint [<path>]      Bronze-level conformance check
  axi ext validate [<path>]  Deeper checks (entry points, tests, public API)
  axi ext test [<path>]      Run the extension's tests

Legacy verbs (predate AEOS) are retained for backward compatibility:

  axi ext check <name>       Legacy per-extension validator (see `axi ext lint`)
  axi ext mcp [--target]     Generate MCP client config

``docs`` used to live here too; it is now fully owned by the Provider
framework (see :mod:`axiom.cli.ext.commands.docs`) and generates
``<ext>/docs/EXTENSION_CONTRACTS.md`` from the AEOS manifest.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Lifecycle grouping for the top-level ``axi ext --help`` listing. Verbs
# mentioned here are grouped under the corresponding header; anything not
# listed falls under "Other" so newly-added verbs don't disappear silently.
LIFECYCLE_GROUPS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("Scaffold", ("quickstart", "init", "templates")),
    (
        "Iterate",
        ("lint", "validate", "test", "doctor", "docs", "config", "graph",
         "run", "eval", "migrate"),
    ),
    ("Publish", ("scan", "sign", "verify", "publish")),
    (
        "Consume",
        ("list", "search", "show", "install", "uninstall", "update"),
    ),
    ("Diagnostic", ("whoami", "status", "completion")),
    ("Legacy", ("check", "mcp")),
)


class _GroupedHelpFormatter(argparse.HelpFormatter):
    """Render the top-level subcommand listing grouped by lifecycle stage.

    We override ``_format_action`` for the root subparsers action and emit a
    custom block instead of argparse's default flat table. Per-verb help
    (``axi ext <verb> --help``) is unaffected — argparse handles that for
    us via the existing subparsers.
    """

    def _format_action(self, action: argparse.Action) -> str:
        if not isinstance(action, argparse._SubParsersAction):
            return super()._format_action(action)

        # Build a map of verb -> help string from the subparser choices.
        help_by_verb: dict[str, str] = {}
        for choice_action in action._choices_actions:
            help_by_verb[choice_action.dest] = (choice_action.help or "").strip()

        lines: list[str] = []
        remaining = set(help_by_verb.keys())

        for header, verbs in LIFECYCLE_GROUPS:
            present = [v for v in verbs if v in help_by_verb]
            if not present:
                continue
            lines.append("")
            lines.append(f"{header}:")
            for v in present:
                help_text = help_by_verb.get(v, "")
                lines.append(f"  {v:<12}  {help_text}")
                remaining.discard(v)

        if remaining:
            lines.append("")
            lines.append("Other:")
            for v in sorted(remaining):
                help_text = help_by_verb.get(v, "")
                lines.append(f"  {v:<12}  {help_text}")

        # Trailing newline for visual separation from the "optional arguments"
        # block argparse prints after subcommands.
        lines.append("")
        return "\n".join(lines) + "\n"


def get_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="axi ext",
        description="Manage AEOS-conformant extensions",
        formatter_class=_GroupedHelpFormatter,
    )
    sub = parser.add_subparsers(dest="action")

    # AEOS-aligned verbs (Tier 1 + Tier 2) — dispatched via the Provider
    # framework. Each provider registers its own flags on demand.
    from axiom.cli.ext import discover_providers

    for verb, provider in discover_providers().items():
        verb_parser = sub.add_parser(verb, help=provider.description)
        provider.add_arguments(verb_parser)

    # axi ext check <name> (legacy pre-AEOS validator)
    check_p = sub.add_parser("check", help="Legacy pre-AEOS validator (see: lint)")
    check_p.add_argument("name", help="Extension name to validate")

    # axi ext mcp [--target ...] [--path ...]
    mcp_p = sub.add_parser(
        "mcp",
        help="Generate/refresh MCP client config from installed extensions",
    )
    mcp_p.add_argument(
        "--target",
        choices=["claude_code", "cursor", "claude_desktop"],
        default="claude_code",
        help="Target MCP client (default: claude_code)",
    )
    mcp_p.add_argument(
        "--path",
        default="",
        help="Output path (default: .mcp.json for claude_code)",
    )
    mcp_p.add_argument(
        "--dry-run", action="store_true",
        help="Print the generated config without writing",
    )

    return parser


def _cmd_init_legacy(args: argparse.Namespace) -> None:
    """(Deprecated) Legacy pre-AEOS scaffold.

    The AEOS-conformant scaffold lives in :mod:`axiom.cli.ext.commands.init`
    and is reached through ``axi ext init`` via the Provider framework.
    This function is retained so legacy callers of
    ``axiom.extensions.scaffold.scaffold_extension`` keep working.
    """
    from axiom.extensions.scaffold import scaffold_extension

    base_dir = Path(args.dir) if args.dir else None

    try:
        ext_dir = scaffold_extension(
            args.name,
            base_dir=base_dir,
            author=args.author,
            description=args.description,
        )
    except FileExistsError as e:
        print(f"Error: {e}")
        sys.exit(1)

    print(f"Extension scaffolded: {ext_dir}")
    print()
    print("Created:")
    print(f"  {ext_dir}/axiom-extension.toml     Manifest")
    print(f"  {ext_dir}/tools_ext/              Chat tools")
    print(f"  {ext_dir}/skills/weekly-slides/   SKILL.md")
    print(f"  {ext_dir}/providers/              Docflow providers")
    print(f"  {ext_dir}/cli/                    CLI commands")
    print(f"  {ext_dir}/extractors/             Sense extractors")
    print()
    print("Next steps:")
    print("  axi ext                    # Verify it appears")
    print(f"  axi ext check {args.name}  # Validate")
    print("  axi chat                   # Chat tools are available immediately")


def _cmd_check(args: argparse.Namespace) -> None:
    """Validate an extension."""
    from axiom.extensions.contracts import validate_extension
    from axiom.extensions.discovery import discover_extensions

    extensions = discover_extensions()
    ext = next((e for e in extensions if e.name == args.name), None)

    if ext is None:
        print(f"Extension not found: {args.name}")
        print()
        print("Installed extensions:")
        for e in extensions:
            print(f"  {e.name}")
        if not extensions:
            print("  (none)")
        sys.exit(1)

    issues = validate_extension(ext)

    if issues:
        print(f"Validation failed for {args.name}:")
        for issue in issues:
            print(f"  - {issue}")
        sys.exit(1)
    else:
        print(f"Extension {args.name} is valid.")
        print()
        print(f"  Root:         {ext.root}")
        print(f"  Version:      {ext.version}")
        print(f"  Author:       {ext.author or '(not set)'}")
        print(f"  Capabilities: {', '.join(ext.capabilities) or '(none)'}")


def _default_mcp_path(target: str) -> Path:
    """Default output path for each supported MCP target."""
    if target == "claude_code":
        return Path(".mcp.json")
    if target == "cursor":
        return Path(".cursor/mcp.json")
    if target == "claude_desktop":  # pragma: no cover — platform-specific
        home = Path.home()
        return home / "Library" / "Application Support" / "Claude" / "claude_desktop_config.json"
    return Path(".mcp.json")


def _cmd_mcp(args: argparse.Namespace) -> None:
    """Generate (or refresh) MCP client config from extension manifests."""
    import json as _json

    from axiom.extensions.discovery import discover_extensions
    from axiom.extensions.mcp_generation import (
        MCPTarget,
        build_mcp_config,
        write_mcp_config,
    )

    extensions = discover_extensions()
    target = MCPTarget(args.target)
    out_path = Path(args.path) if args.path else _default_mcp_path(args.target)

    if args.dry_run:
        config = build_mcp_config(extensions, target=target)
        print(_json.dumps(config, indent=2))
        return

    merged = write_mcp_config(out_path, extensions, target=target)
    n_servers = len(merged.get("mcpServers", {}))
    n_from_exts = sum(1 for e in extensions if e.mcp_servers)
    print(f"Wrote {out_path} ({n_servers} MCP server(s) from {n_from_exts} extension(s))")


def main():
    parser = get_parser()
    args = parser.parse_args()

    # AEOS-aligned verbs go through the Provider framework first.
    from axiom.cli.ext import CliContext, discover_providers

    providers = discover_providers()
    if args.action in providers:
        provider = providers[args.action]
        ctx = CliContext(cwd=Path.cwd())
        sys.exit(provider.run(args, ctx))

    # No subcommand -> dispatch to ``axi ext list`` (the Provider owns this).
    if args.action is None:
        list_provider = providers.get("list")
        if list_provider is not None:
            # The list provider reads attributes defensively via ``getattr``,
            # so an empty Namespace is safe here — no need to re-parse.
            ctx = CliContext(cwd=Path.cwd())
            sys.exit(list_provider.run(argparse.Namespace(), ctx))
        # Fallback should be unreachable (ListProvider is a builtin), but
        # keep the help-print for graceful failure if someone strips it.
        parser.print_help()
        sys.exit(0)

    # Legacy verbs retained for backward compatibility.
    if args.action == "check":
        _cmd_check(args)
    elif args.action == "mcp":
        _cmd_mcp(args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
