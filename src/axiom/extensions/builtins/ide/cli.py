# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""CLI handler for `axi ide` — IDE setup and configuration.

Usage:
    axi ide status        Show detected IDEs and extension status
    axi ide setup         Auto-configure all detected IDEs
    axi ide extensions    Install recommended extensions
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="axi ide",
        description="Configure your IDE for the best Axiom experience",
    )
    sub = parser.add_subparsers(dest="action")

    sub.add_parser("status", help="Show detected IDEs and extension status")
    setup_p = sub.add_parser("setup", help="Auto-configure all detected IDEs")
    setup_p.add_argument("--no-extensions", action="store_true", help="Skip extension installation")
    sub.add_parser("extensions", help="Install recommended extensions")

    syntax_p = sub.add_parser("syntax", help="Install physics code syntax highlighting")
    syntax_p.add_argument(
        "target",
        nargs="?",
        default="local",
        help="'local' (default) or 'user@host' for SSH remote (e.g., nick@lonestar)",
    )

    parser.add_argument("--json", action="store_true", help="JSON output")

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if not args.action:
        args.action = "status"

    handlers = {
        "status": _cmd_status,
        "setup": _cmd_setup,
        "extensions": _cmd_extensions,
        "syntax": _cmd_syntax,
    }
    return handlers[args.action](args)


def _cmd_status(args) -> int:
    from axiom.infra.ide import detect_ides

    ides = detect_ides()

    if getattr(args, "json", False):
        print(json.dumps([ide.to_dict() for ide in ides], indent=2))
        return 0

    print("IDE Status")
    print("=" * 60)
    for ide in ides:
        if ide.installed:
            ver = f" ({ide.version})" if ide.version else ""
            print(f"  \033[32m{ide.name}\033[0m{ver}")
            if ide.extensions_missing:
                print(f"    Missing: {', '.join(ide.extensions_missing)}")
                print("    Run: axi ide extensions")
            elif ide.extensions_installed:
                print("    All recommended extensions installed")
        else:
            print(f"  \033[90m{ide.name}\033[0m — not installed")

    print()
    print("Run `axi ide setup` to auto-configure all detected IDEs.")
    return 0


def _cmd_setup(args) -> int:
    from axiom.infra.ide import setup_ide

    # Find project root
    root = _find_project_root()
    schemas = _collect_schemas(root)
    auto_ext = not getattr(args, "no_extensions", False)

    print("Setting up IDE configuration...")
    result = setup_ide(root, schemas=schemas, auto_install_extensions=auto_ext)

    if result["ides_detected"]:
        print(f"\n  IDEs configured: {', '.join(result['ides_detected'])}")
    else:
        print("\n  No supported IDEs detected.")

    if result["configs_written"]:
        for config in result["configs_written"]:
            print(f"  Wrote: {config}")

    if result["extensions_installed"]:
        print(f"  Extensions installed: {', '.join(result['extensions_installed'])}")

    print("\nDone. Restart your IDE to pick up changes.")
    return 0


def _cmd_extensions(args) -> int:
    from axiom.infra.ide import detect_ides, install_extensions

    ides = detect_ides()
    total = 0

    for ide in ides:
        if not ide.installed or not ide.extensions_missing:
            continue

        print(f"Installing extensions for {ide.name}...")
        installed = install_extensions(ide.binary, ide.extensions_missing)
        for ext in installed:
            print(f"  Installed: {ext}")
            total += 1

    if total == 0:
        print("All recommended extensions already installed.")
    else:
        print(f"\nInstalled {total} extension(s). Restart your IDE.")
    return 0


def _cmd_syntax(args) -> int:
    from axiom.infra.ide import install_vim_syntax

    target = getattr(args, "target", "local") or "local"

    if target == "local":
        print("Installing physics code syntax highlighting (local)...")
    else:
        print(f"Installing physics code syntax highlighting on {target}...")

    installed = install_vim_syntax(target)

    if installed:
        for f in installed:
            print(f"  Installed: {f}")
        if target == "local":
            print("\nOpen a .i or .inp file in vim/neovim to see MCNP highlighting.")
        else:
            print(f"\nSSH to {target} and open a .i file in vi to see highlighting.")
    else:
        print("No syntax files to install.")
    return 0


def _find_project_root() -> Path:
    """Find the project root (has pyproject.toml or .git)."""
    cwd = Path.cwd()
    for parent in [cwd, *cwd.parents]:
        if (parent / "pyproject.toml").exists() or (parent / ".git").exists():
            return parent
    return cwd


def _collect_schemas(root: Path) -> dict[str, str]:
    """Find JSON Schema files in the project for IDE schema association."""
    schemas = {}
    for schema_file in root.rglob("*-schema.json"):
        uri = schema_file.resolve().as_uri()
        # Associate with the YAML file the schema validates
        stem = schema_file.stem.replace("-schema", "")
        schemas[uri] = f"{stem}.yaml"
    return schemas


if __name__ == "__main__":
    sys.exit(main())
