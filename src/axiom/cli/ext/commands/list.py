# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""``axi ext list`` — unified view of pip-installed + axi-installed extensions.

This provider supersedes the legacy ``_cmd_list`` in
:mod:`axiom.extensions.cli`. Two sources of truth are merged:

1. **pip source** — :func:`axiom.extensions.discovery.discover_extensions`
   for anything installed via a normal Python package (builtin, user dir,
   or site-packages).
2. **axi source** — :func:`axiom.cli.ext.install_state.list_installed`
   for extensions laid down through ``axi ext install``.

An extension in both sources is displayed once with ``SOURCE=both``. An
extension whose ``install_path`` no longer exists on disk is flagged
``STATUS=missing`` so the user can either re-install or ``drop_install``.

Tests monkeypatch :func:`_pip_source` when they want deterministic pip
output without touching the real site-packages.
"""

from __future__ import annotations

import argparse
import json
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from axiom.cli.ext._output import console, next_steps
from axiom.cli.ext.install_state import InstallRecord, list_installed
from axiom.cli.ext.provider import CliContext

# Column headers — kept in one place so JSON keys + table columns agree.
_COLUMNS = ("NAME", "VERSION", "SOURCE", "STATUS")


@dataclass(frozen=True)
class ListRow:
    """A single row in the ``axi ext list`` output."""

    name: str
    version: str
    source: str  # "pip" | "axi" | "both"
    status: str  # "enabled" | "disabled" | "installed" | "missing"

    def to_json(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "version": self.version,
            "source": self.source,
            "status": self.status,
        }


@dataclass(frozen=True)
class _PipEntry:
    """Minimal projection of an :class:`axiom.extensions.contracts.Extension`.

    We carry only the fields the list view uses. Indirection lets tests
    stub out :func:`_pip_source` without needing to construct real
    :class:`Extension` objects or plant fake manifests on disk.
    """

    name: str
    version: str
    enabled: bool


def _pip_source() -> list[_PipEntry]:
    """Return pip-installed extensions as a small projection.

    Tests monkeypatch this function directly. Keeping the heavy
    :func:`discover_extensions` call behind a tiny seam keeps the test
    surface small.
    """
    # `ext list` is a *listing* surface, so it uses the brand-scoped view
    # (ADR-048): a sibling product's extensions stay invocable but aren't shown
    # under another brand. Resolution paths use discover_extensions().
    from axiom.extensions.discovery import surfaced_extensions

    result: list[_PipEntry] = []
    try:
        extensions = surfaced_extensions()
    except Exception:
        # Discovery is best-effort — a broken site-packages shouldn't make
        # ``axi ext list`` crash.
        return result
    for ext in extensions:
        result.append(_PipEntry(name=ext.name, version=ext.version, enabled=ext.enabled))
    return result


def _axi_source() -> list[InstallRecord]:
    """Return axi-installed records. Thin wrapper so tests can monkeypatch."""
    return list_installed()


def _axi_status(record: InstallRecord) -> str:
    """Map an install record to ``installed`` or ``missing``."""
    path = Path(record.install_path)
    return "installed" if path.exists() else "missing"


def build_rows(
    *,
    pip_source: Callable[[], list[_PipEntry]] | None = None,
    axi_source: Callable[[], list[InstallRecord]] | None = None,
    source_filter: str = "all",
) -> list[ListRow]:
    """Build the unified rows. Public so tests can drive it directly."""
    pip_source = pip_source or _pip_source
    axi_source = axi_source or _axi_source

    pip_entries = {e.name: e for e in pip_source()}
    axi_entries = {r.name: r for r in axi_source()}

    names = sorted(set(pip_entries) | set(axi_entries))
    rows: list[ListRow] = []
    for name in names:
        pip_entry = pip_entries.get(name)
        axi_entry = axi_entries.get(name)

        if pip_entry is not None and axi_entry is not None:
            source = "both"
            version = axi_entry.version or pip_entry.version
            # Pip's enabled/disabled flag wins for dual-source rows — if
            # the user disabled the pip copy we should say so.
            status = "enabled" if pip_entry.enabled else "disabled"
        elif pip_entry is not None:
            source = "pip"
            version = pip_entry.version
            status = "enabled" if pip_entry.enabled else "disabled"
        else:
            assert axi_entry is not None  # exactly one of the three cases
            source = "axi"
            version = axi_entry.version
            status = _axi_status(axi_entry)

        # Source filter.
        if source_filter == "pip" and source == "axi":
            continue
        if source_filter == "axi" and source == "pip":
            continue
        # ``both`` rows survive either filter — they belong to both sources.

        rows.append(
            ListRow(name=name, version=version, source=source, status=status)
        )
    return rows


def _format_table(rows: list[ListRow]) -> str:
    if not rows:
        return ""
    name_w = max(len(_COLUMNS[0]), *(len(r.name) for r in rows))
    ver_w = max(len(_COLUMNS[1]), *(len(r.version) for r in rows))
    src_w = max(len(_COLUMNS[2]), *(len(r.source) for r in rows))
    status_w = max(len(_COLUMNS[3]), *(len(r.status) for r in rows))
    header = (
        f"{_COLUMNS[0].ljust(name_w)}  "
        f"{_COLUMNS[1].ljust(ver_w)}  "
        f"{_COLUMNS[2].ljust(src_w)}  "
        f"{_COLUMNS[3].ljust(status_w)}"
    )
    sep = "-" * len(header)
    lines = [header, sep]
    for r in rows:
        lines.append(
            f"{r.name.ljust(name_w)}  "
            f"{r.version.ljust(ver_w)}  "
            f"{r.source.ljust(src_w)}  "
            f"{r.status.ljust(status_w)}"
        )
    return "\n".join(lines)


class ListProvider:
    """Built-in provider for ``axi ext list``.

    Also serves as the default dispatch target when ``axi ext`` is run
    with no subcommand (see :func:`axiom.extensions.cli.main`).
    """

    verb = "list"
    description = "List installed extensions (pip + axi-managed)"

    def add_arguments(self, parser: argparse.ArgumentParser) -> None:
        parser.add_argument(
            "--json",
            dest="as_json",
            action="store_true",
            help="Emit results as JSON",
        )
        parser.add_argument(
            "--source",
            choices=("pip", "axi", "all"),
            default="all",
            help="Filter by originating source (default: all)",
        )

    def run(self, args: argparse.Namespace, context: CliContext) -> int:
        rows = build_rows(source_filter=getattr(args, "source", "all"))

        if getattr(args, "as_json", False):
            print(
                json.dumps(
                    {"extensions": [r.to_json() for r in rows]},
                    indent=2,
                    sort_keys=True,
                )
            )
            return 0

        con = console()
        if not rows:
            con.print("No extensions installed.")
            con.print("")
            next_steps(
                [
                    "axi ext init my-extension    # Create a new extension",
                    "axi ext search <query>       # Find an extension to install",
                    "axi ext install <name>       # Install from the registry",
                ],
                header="Get started:",
            )
            return 0

        con.print(_format_table(rows))
        con.print("")
        n_pip = sum(1 for r in rows if r.source in ("pip", "both"))
        n_axi = sum(1 for r in rows if r.source in ("axi", "both"))
        parts = []
        if n_pip:
            parts.append(f"{n_pip} pip")
        if n_axi:
            parts.append(f"{n_axi} axi")
        summary = ", ".join(parts) if parts else "0"
        con.print(f"{len(rows)} extension(s) listed ({summary}).")
        return 0


__all__ = ["ListProvider", "ListRow", "build_rows"]
