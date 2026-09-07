# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""``axi ext status`` — welcome + dashboard.

Two modes dispatched off the current state:

- **Welcome** — nothing created yet (no ``$AXIOM_HOME``, no installs, no
  publisher key): the user sees the three lifecycle entry points so
  first-run has an obvious next action.
- **Dashboard** — there's state: counts per lifecycle bucket (registry
  entries, pip extensions, axi installs), the publisher fingerprint, and
  the active registry URL so authoring + consuming users both get a
  crisp summary.

``--json`` emits the same facts structurally so scripts can consume them.
"""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from axiom.cli.ext._output import console, heading, next_steps
from axiom.cli.ext.commands.whoami import _pip_installed_count, build_summary
from axiom.cli.ext.install_state import list_installed
from axiom.cli.ext.provider import CliContext
from axiom.cli.ext.registry_backend import read_index


@dataclass
class StatusDashboard:
    """Structural view of ``axi ext status``."""

    mode: str  # "welcome" | "dashboard"
    axiom_home: str
    axiom_home_exists: bool
    publisher_key_fingerprint: str
    registry_url: str
    registry_entry_count: int
    registry_latest_publish: dict[str, str] = field(default_factory=dict)
    installed_axi: int = 0
    installed_pip: int = 0
    oldest_install_name: str = ""
    oldest_install_when: str = ""

    def to_json(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "axiom_home": self.axiom_home,
            "axiom_home_exists": self.axiom_home_exists,
            "publisher_key_fingerprint": self.publisher_key_fingerprint,
            "registry_url": self.registry_url,
            "registry_entry_count": self.registry_entry_count,
            "registry_latest_publish": dict(self.registry_latest_publish),
            "installed_axi": self.installed_axi,
            "installed_pip": self.installed_pip,
            "oldest_install_name": self.oldest_install_name,
            "oldest_install_when": self.oldest_install_when,
        }


def _registry_counts() -> tuple[int, dict[str, str]]:
    """Return (# entries, latest published {name: version})."""
    try:
        idx = read_index()
    except Exception:  # noqa: BLE001 — a broken registry shouldn't crash status
        return 0, {}
    exts = idx.get("extensions") or {}
    latest: dict[str, str] = {}
    for name, entry in exts.items():
        ver = (entry or {}).get("latest")
        if ver:
            latest[name] = str(ver)
    return len(exts), latest


def _oldest_install() -> tuple[str, str]:
    """Return (name, installed_at) for the oldest ``axi ext install``."""
    records = list_installed()
    if not records:
        return "", ""
    ordered = sorted(records, key=lambda r: r.installed_at or "")
    first = ordered[0]
    return first.name, first.installed_at


def build_dashboard() -> StatusDashboard:
    """Build the structural view. Public so tests can assert on it directly."""
    summary = build_summary()
    axiom_home_path = Path(summary.axiom_home)
    axi_records = list_installed()
    pip_count = _pip_installed_count()
    registry_count, latest_pub = _registry_counts()

    has_state = (
        axiom_home_path.exists()
        or bool(summary.publisher_key_fingerprint)
        or bool(axi_records)
    )
    mode = "dashboard" if has_state else "welcome"

    oldest_name, oldest_when = _oldest_install()

    return StatusDashboard(
        mode=mode,
        axiom_home=summary.axiom_home,
        axiom_home_exists=axiom_home_path.exists(),
        publisher_key_fingerprint=summary.publisher_key_fingerprint,
        registry_url=summary.registry_url,
        registry_entry_count=registry_count,
        registry_latest_publish=latest_pub,
        installed_axi=len(axi_records),
        installed_pip=pip_count,
        oldest_install_name=oldest_name,
        oldest_install_when=oldest_when,
    )


def _render_welcome(d: StatusDashboard) -> None:
    con = console()
    heading("Welcome to axi ext")
    con.print("")
    con.print(
        f"$AXIOM_HOME at {d.axiom_home} is empty. Pick a starting point:"
    )
    con.print("")
    next_steps(
        [
            "axi ext quickstart <name> # Scaffold + lint + validate + scan in one",
            "axi ext init <name>       # Scaffold only (wizard with --interactive)",
            "axi ext search <query>    # Find extensions in the registry",
        ],
        header="Lifecycle entry points:",
    )


def _render_dashboard(d: StatusDashboard) -> None:
    con = console()
    heading("axi ext status")
    con.print("")
    con.print(f"$AXIOM_HOME:       {d.axiom_home}")
    con.print(f"registry:          {d.registry_url}")
    con.print(
        f"publisher key:     "
        f"{d.publisher_key_fingerprint or '(not yet created)'}"
    )
    con.print(f"registry entries:  {d.registry_entry_count}")
    if d.registry_latest_publish:
        recent = ", ".join(
            f"{k}@{v}" for k, v in sorted(d.registry_latest_publish.items())[:3]
        )
        con.print(f"recent publishes:  {recent}")
    con.print(
        f"installed:         {d.installed_axi} axi-managed, "
        f"{d.installed_pip} pip-discovered"
    )
    if d.oldest_install_name:
        con.print(
            f"oldest install:    {d.oldest_install_name} "
            f"({d.oldest_install_when})"
        )


class StatusProvider:
    """Built-in provider for ``axi ext status``."""

    verb = "status"
    description = "Welcome view on first run; dashboard summary afterwards"

    def add_arguments(self, parser: argparse.ArgumentParser) -> None:
        parser.add_argument(
            "--json",
            dest="as_json",
            action="store_true",
            help="Emit the dashboard as JSON",
        )

    def run(self, args: argparse.Namespace, context: CliContext) -> int:
        dashboard = build_dashboard()
        if getattr(args, "as_json", False):
            console().print(
                json.dumps(dashboard.to_json(), indent=2, sort_keys=True)
            )
            return 0
        if dashboard.mode == "welcome":
            _render_welcome(dashboard)
        else:
            _render_dashboard(dashboard)
        return 0


__all__ = ["StatusDashboard", "StatusProvider", "build_dashboard"]
