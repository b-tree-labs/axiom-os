# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""``axi ext activate`` — guided, idempotent activation (ADR-005 rung 4).

The step that makes the 2026-09-11 console-ingest 422 impossible: a LOUD
pre-flight lists every missing activation step (connector registration, conform
mapping, timer) before touching anything, repairs the repairable ones, and
re-checks; ``--env prod`` gates on RACI (``extension.promote``)."""
from __future__ import annotations

import argparse
from pathlib import Path

from axiom.cli.ext._output import console
from axiom.cli.ext.lifecycle import activate, real_deps
from axiom.cli.ext.provider import CliContext


class ActivateProvider:
    """Built-in provider for ``axi ext activate [<path>]``."""

    verb = "activate"
    description = "Guided activation (register connector / arm timer); RACI-gated for --env prod"

    def add_arguments(self, parser: argparse.ArgumentParser) -> None:
        parser.add_argument("path", nargs="?", default=None,
                            help="Path to the extension (default: current working directory)")
        parser.add_argument("--env", choices=["staging", "prod"], default="staging",
                            help="Target environment (prod is RACI-gated)")
        parser.add_argument("--site", default="ut-triga", help="Site key for the conform mapping")
        parser.add_argument("--check-only", action="store_true",
                            help="Pre-flight only: list missing steps and change nothing")

    def run(self, args: argparse.Namespace, context: CliContext) -> int:
        ext_dir = Path(args.path).resolve() if args.path else context.cwd
        deps = real_deps()
        res = activate(ext_dir, args.env, registry=deps["registry"], systemd=deps["systemd"],
                       raci=deps["raci"], herald=deps["herald"], site=args.site,
                       check_only=args.check_only)
        con = console()
        for m in res.messages:
            con.print(m)
        return 0 if res.ok else 1
