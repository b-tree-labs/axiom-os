# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""CLI handler for axi config.

Usage:
    axi config                Run full wizard (or resume if state exists)
    axi config --status       Show current configuration status
    axi config --set <name>   Configure a specific connection
    axi config --reset        Clear state and start over
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from axiom.setup.state import clear_state
from axiom.setup.wizard import SetupWizard

# ---------------------------------------------------------------------------
# axi config → axi settings setup alias (spec-settings §3.6)
# ---------------------------------------------------------------------------


def _alias_banner_marker() -> Path:
    """Per-session marker so the migration banner shows at most once.

    Patched in tests; in production it's a tmpfs-ish path tied to the
    process tree so the banner reappears in the next shell session.
    """
    import os
    import tempfile
    base = Path(tempfile.gettempdir()) / "axiom"
    base.mkdir(exist_ok=True)
    return base / f"axi-config-alias-banner-{os.getppid()}"


def run_settings_setup() -> int:
    """Invoke the unified `axi settings setup` wizard chain.

    Thin shim: discover registered sections, run each one's wizard in
    section order via the settings CLI helper.
    """
    from axiom.extensions.builtins.settings.cli import cmd_settings_setup
    from axiom.infra.settings_sections import discover_settings_sections

    return cmd_settings_setup(discover_settings_sections())


def alias_to_settings_setup() -> int:
    """Entry point for `axi config` when used as an alias.

    Emits a once-per-session migration banner pointing users at the new
    surface, then delegates to `run_settings_setup`.
    """
    marker = _alias_banner_marker()
    if not marker.exists():
        print(
            "\n   ℹ `axi config` now runs `axi settings setup`.\n"
            "     Per-area config: `axi settings <section>`.\n"
        )
        try:
            marker.touch()
        except OSError:
            pass  # banner reappears next time; not worth failing the wizard
    return run_settings_setup()


def get_parser() -> argparse.ArgumentParser:
    """Build and return the argument parser.

    Exposed for CLI registry introspection and argcomplete.
    """
    from axiom.setup.llamafile import MODELS

    parser = argparse.ArgumentParser(
        prog="axi config",
        description="Interactive onboarding wizard",
    )
    parser.add_argument("--status", action="store_true", help="Show configuration status")
    parser.add_argument("--set", metavar="NAME", help="Configure a specific connection")
    parser.add_argument("--reset", action="store_true", help="Clear state and start over")
    parser.add_argument(
        "--model",
        choices=sorted(MODELS.keys()),
        default=None,
        help=(
            "Local LLM profile to provision (default: qwen). "
            "Pass 'bonsai' for the legacy 1.7GB option."
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Resolve the local LLM profile and exit without running setup.",
    )
    return parser


def main() -> None:
    """Entry point for `axi config`.

    As of spec-settings §3.6, `axi config` (no args) is an alias for
    `axi settings setup` — see `alias_to_settings_setup`. Legacy flags
    (--status, --set, --reset, --model, --dry-run) keep working until
    the next deprecation window.
    """
    raw = sys.argv[1:]

    # ADR-065 PR-1: divert `axi config {validate|show|emit-schema}` to the
    # schema-bilingual config verbs. Bare `axi config` and the legacy
    # flag-form invocations keep their existing wizard behaviour below.
    if raw and raw[0] in ("validate", "show", "emit-schema"):
        from axiom.infra.config.cli import main as _config_main

        sys.exit(_config_main(raw))

    if "--help" in raw or "-h" in raw:
        _print_help()
        return

    if not raw:
        # Bare `axi config` → unified alias. Legacy flag invocations
        # continue through the original SetupWizard path below.
        alias_to_settings_setup()
        return

    parser = get_parser()
    args = parser.parse_args(raw)

    if args.reset:
        clear_state()
        print("  Setup state cleared. Run 'axi config' to start fresh.")
        return

    wizard = SetupWizard(model=args.model)

    if args.dry_run:
        # Print the resolved local LLM profile and exit. Does not download
        # anything or write state. Used as a smoke check.
        from axiom.setup.llamafile import resolve_model

        model = wizard.resolve_local_model()
        profile = resolve_model(model)
        print(f"  Local LLM profile: {model}")
        print(f"    gguf: {profile['gguf']}")
        print(f"    id:   {profile['id']}")
        print(f"    size: {profile['size_gb']}GB")
        print(f"    url:  {profile['url']}")
        return

    if args.status:
        wizard.show_status()
        return

    if args.set is not None:
        wizard.fix(args.set)
        return

    # Default: run the full wizard
    try:
        wizard.run()
    except KeyboardInterrupt:
        print("\n\n  Setup paused. Run 'axi config' to resume.\n")
        sys.exit(130)


def _print_help() -> None:
    print("axi config — Interactive onboarding wizard")
    print()
    print("Usage:")
    print("  axi config              Run full wizard (or resume)")
    print("  axi config --status     Show current configuration status")
    print("  axi config --set NAME   Configure a specific connection")
    print("  axi config --reset      Clear state and start over")
    print()
    print("Connections:")
    from axiom.setup.guides import CREDENTIAL_GUIDES  # pylint: disable=import-outside-toplevel
    for g in CREDENTIAL_GUIDES:
        tag = "required" if g.required else "optional"
        print(f"  {g.env_var.lower():<30s} {g.display_name} ({tag})")
