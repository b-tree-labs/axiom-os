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

    Bare-install fallback (sandbox audit 2026-09-18, gap 1): when no
    extension registers a section wizard — which is exactly the state of a
    fresh `pip install` — the section chain would print a banner, run zero
    wizards, and exit 0 having provisioned nothing, while `chat`'s fix
    advice points back here. In that case run the full onboarding wizard
    instead; its skip-everything flow already handles a bare machine.
    """
    from axiom.extensions.builtins.settings.cli import cmd_settings_setup
    from axiom.infra.settings_sections import discover_settings_sections

    defs = discover_settings_sections()
    if not any(d.wizard for d in defs):
        print("   No per-section wizards registered yet — running the onboarding wizard.")
        try:
            SetupWizard().run()
        except KeyboardInterrupt:
            print(f"\n\n  Setup paused. Run '{_brand_cli()} config' to resume.\n")
            return 130
        return 0
    return cmd_settings_setup(defs)


def alias_to_settings_setup() -> int:
    """Entry point for `axi config` when used as an alias.

    Emits a once-per-session migration banner pointing users at the new
    surface, then delegates to `run_settings_setup`.
    """
    marker = _alias_banner_marker()
    if not marker.exists():
        print(
            f"\n   ℹ `{_brand_cli()} config` now runs `{_brand_cli()} settings setup`.\n"
            f"     Per-area config: `{_brand_cli()} settings <section>`.\n"
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
        prog=f"{_brand_cli()} config",
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
            "Pass 'small' for the lightweight gemma2-2b option (~1.6GB)."
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
        print(f"  Setup state cleared. Run '{_brand_cli()} config' to start fresh.")
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
        print(f"\n\n  Setup paused. Run '{_brand_cli()} config' to resume.\n")
        sys.exit(130)


def _print_help() -> None:
    from axiom.setup.llamafile import DEFAULT_MODEL, MODELS, SMALL_MODEL

    default_size = MODELS[DEFAULT_MODEL]["size_gb"]
    small_size = MODELS[SMALL_MODEL]["size_gb"]
    print(f"{_brand_cli()} config — Interactive onboarding wizard")
    print()
    print("Usage:")
    print(f"  {_brand_cli()} config              Run full wizard (or resume)")
    print(f"  {_brand_cli()} config --status     Show current configuration status")
    print(f"  {_brand_cli()} config --set NAME   Configure a specific connection")
    print(f"  {_brand_cli()} config --reset      Clear state and start over")
    print(f"  {_brand_cli()} config --model NAME Local LLM profile to provision")
    print(f"                          Default: {DEFAULT_MODEL} ({default_size}GB download)")
    print(f"                          Lightweight: {SMALL_MODEL} ({small_size}GB download)")
    print(f"  {_brand_cli()} config --dry-run    Show the resolved LLM profile and its")
    print("                          download size without downloading anything")
    print()
    print("Connections:")
    from axiom.setup.guides import CREDENTIAL_GUIDES  # pylint: disable=import-outside-toplevel
    for g in CREDENTIAL_GUIDES:
        tag = "required" if g.required else "optional"
        print(f"  {g.env_var.lower():<30s} {g.display_name} ({tag})")
