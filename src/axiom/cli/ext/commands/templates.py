# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""``axi ext templates`` — list scaffolding templates available to ``init``.

Per AEOS §10.1 this is a Tier 1 lifecycle command: it surfaces the starting
points users can pick from with ``axi ext init --template <id>``. The
template registry is in :mod:`axiom.cli.ext.templates`.
"""

from __future__ import annotations

import argparse
import json

from axiom.cli.ext._output import console
from axiom.cli.ext.provider import CliContext
from axiom.cli.ext.templates import registry


class TemplatesProvider:
    """Built-in provider for ``axi ext templates``."""

    verb = "templates"
    description = "List available extension templates"

    def add_arguments(self, parser: argparse.ArgumentParser) -> None:
        parser.add_argument(
            "--json",
            dest="as_json",
            action="store_true",
            help="Emit the template registry as JSON for scripting",
        )

    def run(self, args: argparse.Namespace, context: CliContext) -> int:
        templates = sorted(registry(), key=lambda t: (not t.is_default, t.id))

        con = console()
        if args.as_json:
            payload = [
                {"id": t.id, "description": t.description, "is_default": t.is_default}
                for t in templates
            ]
            con.print(json.dumps(payload, indent=2))
            return 0

        # Human-readable table.
        id_width = max((len(t.id) for t in templates), default=0)
        id_width = max(id_width, len("ID"))
        header = f"{'ID':<{id_width}}  DESCRIPTION"
        con.print(header)
        con.print("-" * len(header))
        for t in templates:
            marker = " (default)" if t.is_default else ""
            con.print(f"{t.id:<{id_width}}  {t.description}{marker}")
        con.print("")
        con.print("Use:  axi ext init <name> --template <ID>")
        return 0


__all__ = ["TemplatesProvider"]
