# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""axi settings — view and edit axi configuration.

Modeled on Claude Code's settings UX:
  axi settings                              show all active settings
  axi settings get routing.default_mode     read a value
  axi settings set routing.default_mode auto
  axi settings --global set routing.cloud_provider openai
  axi settings reset routing.default_mode   remove project override
"""

from __future__ import annotations

import argparse
import sys
from typing import Any

from axiom.infra import settings_sections as _sections_mod
from axiom.infra.settings_sections import (
    SectionView,
    SettingsSectionDef,
    discover_settings_sections,
    load_section_view,
)

from .store import _DEFAULTS, SettingsStore


def discover_settings_sections_for_main() -> list[SettingsSectionDef]:
    """Thin wrapper around discovery so tests can monkeypatch at this module."""
    return discover_settings_sections()


def load_section_views_for_main() -> list[SectionView]:
    """Materialize a SectionView for each discovered section. Skips failures."""
    out: list[SectionView] = []
    for d in discover_settings_sections_for_main():
        v = load_section_view(d)
        if v is not None:
            out.append(v)
    return out

# ---------------------------------------------------------------------------
# Unified-surface commands (spec-settings §3) — new in v1.1.0
# ---------------------------------------------------------------------------


def cmd_settings_list(
    views: list[SectionView],
    show_all: bool = False,
) -> int:
    """List installed-and-configured sections per spec §3.1.

    Sections with `is_active=False` are omitted unless show_all=True
    (spec §2.3 visibility rule).
    """
    # Build a rich.Console per call (per feedback_rich_console_lazy_construction
    # — module-level singletons hide output from pytest's capsys).
    from rich.console import Console
    from rich.table import Table

    console = Console()

    visible = views if show_all else [v for v in views if v.is_active]
    if not visible:
        console.print()
        console.print("  [dim]No active settings sections.[/dim]")
        console.print(
            "  Run [cyan]axi settings --all[/cyan] to include unconfigured sections.\n"
        )
        return 0

    console.print("\n[bold]📋 Active settings[/bold]\n")

    table = Table(
        show_header=True,
        header_style="bold cyan",
        border_style="dim",
        pad_edge=False,
        padding=(0, 1),
    )
    table.add_column("Section", style="bold", no_wrap=True)
    table.add_column("Summary", style="white")
    if show_all:
        table.add_column("Status", style="dim", justify="center", no_wrap=True)

    ordered = sorted(visible, key=lambda x: (x.name != "general", x.name))
    for v in ordered:
        summary = v.summary or "[dim](no summary)[/dim]"
        if show_all:
            badge = "[green]●[/green] active" if v.is_active else "[dim]○ empty[/dim]"
            row_style = None if v.is_active else "dim"
            table.add_row(v.name, summary, badge, style=row_style)
        else:
            table.add_row(v.name, summary)

    console.print(table)
    console.print()
    console.print(
        "  [cyan]axi settings <section>[/cyan]  [dim]drill in to view or edit[/dim]"
    )
    if not show_all:
        console.print(
            "  [cyan]axi settings --all[/cyan]      [dim]include unconfigured sections[/dim]"
        )
    console.print()
    return 0


def cmd_settings_view(view: SectionView) -> int:
    """Show one section's current values per spec §3.2."""
    from rich.console import Console
    from rich.table import Table

    console = Console()
    console.print(f"\n[bold]\\[{view.name}][/bold]")
    if not view.values:
        console.print("  [dim](no values)[/dim]\n")
        return 0

    table = Table(
        show_header=False,
        border_style="dim",
        pad_edge=False,
        padding=(0, 1),
    )
    table.add_column("Key", style="cyan", no_wrap=True)
    table.add_column("Value", style="white")
    for k in sorted(view.values):
        table.add_row(k, _fmt_value(view.values[k]))
    console.print(table)

    console.print()
    console.print(
        f"  [cyan]axi settings {view.name} set <key> <value>[/cyan]  [dim]edit a key[/dim]"
    )
    console.print(
        f"  [cyan]axi settings {view.name} reset <key>[/cyan]        [dim]remove an override[/dim]\n"
    )
    return 0


def cmd_settings_setup(defs: list[SettingsSectionDef]) -> int:
    """Invoke per-section wizards in order per spec §3.4."""
    print("\n   🧭 Axiom setup (wizard)\n")
    for d in defs:
        if not d.wizard:
            continue
        wiz = _sections_mod._resolve_entry(d.wizard)
        if wiz is None:
            print(f"   ⏭ {d.name}: wizard entry not resolvable; skipping")
            continue
        print(f"   ── {d.name} {'─' * max(2, 54 - len(d.name))}")
        try:
            wiz()
        except Exception as exc:  # noqa: BLE001 — wizards must not abort setup
            print(f"   ⚠ {d.name}: wizard raised {exc}; continuing")
    print()
    return 0


def _fmt_value(v: Any) -> str:
    if isinstance(v, bool):
        return str(v).lower()
    return str(v)


def _print_table(settings: dict[str, Any]) -> None:
    """Print settings as a two-column table, grouped by section."""
    from collections import defaultdict

    sections: dict[str, dict[str, Any]] = defaultdict(dict)
    for k, v in sorted(settings.items()):
        parts = k.split(".", 1)
        section = parts[0] if len(parts) > 1 else "general"
        leaf = parts[1] if len(parts) > 1 else parts[0]
        sections[section][leaf] = v

    col_width = max((len(k) for s in sections.values() for k in s), default=20) + 2

    for section, items in sorted(sections.items()):
        print(f"\n  [{section}]")
        for key, val in sorted(items.items()):
            dotted = f"{section}.{key}"
            print(f"    {dotted:<{col_width}}  {_fmt_value(val)}")
    print()


def get_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="axi settings",
        description="View and edit axi configuration",
    )
    parser.add_argument(
        "--global", dest="global_scope", action="store_true",
        help="Operate on global settings (~/.neut/settings.toml)",
    )
    sub = parser.add_subparsers(dest="cmd")

    get_p = sub.add_parser("get", help="Read a setting value")
    get_p.add_argument("key", help="Dotted key, e.g. routing.default_mode")

    set_p = sub.add_parser("set", help="Write a setting value")
    set_p.add_argument("key", help="Dotted key, e.g. routing.default_mode")
    set_p.add_argument("value", help="New value")

    reset_p = sub.add_parser("reset", help="Remove a setting override")
    reset_p.add_argument("key", help="Dotted key to reset")

    sub.add_parser("edit", help="Open settings file in $EDITOR")
    sub.add_parser("setup", help="Run the interactive onboarding wizard")

    return parser


def main():
    parser = get_parser()
    args = parser.parse_args()
    scope = "global" if args.global_scope else "project"
    store = SettingsStore()

    if args.cmd == "setup":
        cmd_settings_setup(discover_settings_sections_for_main())
        return

    if args.cmd is None:
        # Prefer the unified section listing when any extension has
        # registered a section; fall back to the legacy flat table for
        # back-compat (spec-settings §3.6 migration path).
        views = load_section_views_for_main()
        if views:
            cmd_settings_list(views)
            return
        settings = store.all()
        if args.global_scope:
            from .store import _GLOBAL_SETTINGS_PATH, _flatten, _load_toml
            settings = _flatten(_load_toml(_GLOBAL_SETTINGS_PATH))
        if not settings:
            print("\n  No settings configured. Using defaults.\n")
            settings = store.all()
        _print_table(settings)
        return

    if args.cmd == "get":
        val = store.get(args.key)
        if val is None:
            print("  (not set — no default)", file=sys.stderr)
            sys.exit(1)
        print(_fmt_value(val))
        return

    if args.cmd == "set":
        # Coerce common types
        raw = args.value
        if raw.lower() in ("true", "yes"):
            value: Any = True
        elif raw.lower() in ("false", "no"):
            value = False
        elif raw.startswith("[") and raw.endswith("]"):
            # Explicit list syntax: [a, b, c]
            value = [v.strip().strip("'\"") for v in raw[1:-1].split(",") if v.strip()]
        elif isinstance(_DEFAULTS.get(args.key), list):
            # Key's default is a list — parse comma-separated into list
            value = [v.strip() for v in raw.split(",") if v.strip()]
        else:
            try:
                value = int(raw)
            except ValueError:
                value = raw

        store.set(args.key, value, scope=scope)
        target = "~/.neut/settings.toml" if scope == "global" else ".neut/settings.toml"
        print(f"  {args.key} = {_fmt_value(value)}  →  {target}")
        return

    if args.cmd == "reset":
        removed = store.reset(args.key, scope=scope)
        if removed:
            print(f"  Removed override: {args.key} ({scope})")
        else:
            print(f"  {args.key} not set in {scope} scope (nothing to reset)")
        return

    if args.cmd == "edit":
        import os
        import subprocess

        from .store import _GLOBAL_SETTINGS_PATH, _PROJECT_SETTINGS_PATH

        path = _GLOBAL_SETTINGS_PATH if scope == "global" else _PROJECT_SETTINGS_PATH
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            # Seed with current settings so the user has something to edit
            store_snapshot = store.all()
            from .store import _save_toml, _unflatten
            _save_toml(path, _unflatten(store_snapshot))

        editor = os.environ.get("EDITOR", os.environ.get("VISUAL", "vi"))
        subprocess.call([editor, str(path)])
        print(f"  Settings saved to {path}")
        return
