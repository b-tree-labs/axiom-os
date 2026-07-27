# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""``axi ext whoami`` — identity + state summary.

Answers the "who am I, where is my state, what's installed" question that
otherwise requires grepping ``$AXIOM_HOME`` by hand. One line per fact so
the output is cheap to parse by eye or by ``awk``.
"""

from __future__ import annotations

import argparse
import json
import os
from dataclasses import dataclass
from typing import Any

from axiom.cli.ext._output import console
from axiom.cli.ext.commands.config import _axiom_home
from axiom.cli.ext.install_state import list_installed
from axiom.cli.ext.provider import CliContext
from axiom.cli.ext.registry_backend import RegistryPath
from axiom.cli.ext.signing import (
    default_public_key_path,
    public_key_sha256,
    trusted_keys_dir,
)

# ``public_key_sha256`` takes raw PEM bytes, not a path.


@dataclass
class WhoamiSummary:
    """Fields ``whoami`` surfaces. One struct so tests + JSON match."""

    axiom_home: str
    registry_url: str
    registry_source: str
    publisher_key_fingerprint: str  # empty when not yet created
    publisher_key_source: str  # "file" | "missing"
    installed_axi: int
    installed_pip: int
    trusted_publishers: list[str]

    def to_json(self) -> dict[str, Any]:
        return {
            "axiom_home": self.axiom_home,
            "registry_url": self.registry_url,
            "registry_source": self.registry_source,
            "publisher_key_fingerprint": self.publisher_key_fingerprint,
            "publisher_key_source": self.publisher_key_source,
            "installed_axi": self.installed_axi,
            "installed_pip": self.installed_pip,
            "trusted_publishers": list(self.trusted_publishers),
        }


def _pip_installed_count() -> int:
    """Return the number of pip-discoverable extensions. Best-effort."""
    try:
        from axiom.extensions.discovery import discover_extensions

        return len(list(discover_extensions()))
    except Exception:  # noqa: BLE001 — discovery is best-effort
        return 0


def _publisher_fingerprint() -> tuple[str, str]:
    """Return (fingerprint, source) for the user's publisher key."""
    pub = default_public_key_path()
    if not pub.exists():
        return "", "missing"
    try:
        sha = public_key_sha256(pub.read_bytes())
    except Exception:  # noqa: BLE001 — corrupted key file
        return "", "missing"
    return sha, "file"


def _trusted_publishers() -> list[str]:
    """Return the list of fingerprints present in ``$AXIOM_HOME/keys/trusted/``."""
    tdir = trusted_keys_dir()
    if not tdir.exists():
        return []
    out: list[str] = []
    for child in sorted(tdir.iterdir()):
        if child.suffix == ".pub":
            out.append(child.stem)
    return out


def build_summary() -> WhoamiSummary:
    """Gather the state for ``whoami``. Public so tests can drive it directly."""
    axiom_home = str(_axiom_home())

    rp = RegistryPath.resolve()
    registry_url = f"file://{rp.root}"
    registry_source = (
        "AXIOM_REGISTRY_URL"
        if os.environ.get("AXIOM_REGISTRY_URL")
        else "default"
    )

    fingerprint, key_source = _publisher_fingerprint()

    axi_installed = list_installed()
    pip_count = _pip_installed_count()

    return WhoamiSummary(
        axiom_home=axiom_home,
        registry_url=registry_url,
        registry_source=registry_source,
        publisher_key_fingerprint=fingerprint,
        publisher_key_source=key_source,
        installed_axi=len(axi_installed),
        installed_pip=pip_count,
        trusted_publishers=_trusted_publishers(),
    )


def _format_text(s: WhoamiSummary) -> str:
    lines: list[str] = []
    lines.append(f"$AXIOM_HOME:        {s.axiom_home}")
    lines.append(
        f"registry:           {s.registry_url}  "
        f"(set via {s.registry_source})"
    )
    if s.publisher_key_fingerprint:
        lines.append(
            f"publisher key:      {s.publisher_key_fingerprint}"
        )
    else:
        lines.append(
            "publisher key:      not yet created — run `axi ext sign` to create"
        )
    lines.append(
        f"installed count:    {s.installed_axi} axi-managed, "
        f"{s.installed_pip} pip-discovered"
    )
    if s.trusted_publishers:
        shown = ", ".join(s.trusted_publishers[:3])
        more = f" (+{len(s.trusted_publishers) - 3} more)" if len(s.trusted_publishers) > 3 else ""
        lines.append(f"trusted publishers: {len(s.trusted_publishers)}  {shown}{more}")
    else:
        lines.append("trusted publishers: 0")
    return "\n".join(lines)


class WhoamiProvider:
    """Built-in provider for ``axi ext whoami``."""

    verb = "whoami"
    description = "Show current identity, registry, key, and install state"

    def add_arguments(self, parser: argparse.ArgumentParser) -> None:
        parser.add_argument(
            "--json",
            dest="as_json",
            action="store_true",
            help="Emit the summary as JSON",
        )

    def run(self, args: argparse.Namespace, context: CliContext) -> int:
        summary = build_summary()
        con = console()
        if getattr(args, "as_json", False):
            con.print(json.dumps(summary.to_json(), indent=2, sort_keys=True))
            return 0
        con.print(_format_text(summary))
        return 0


__all__ = ["WhoamiProvider", "WhoamiSummary", "build_summary"]
