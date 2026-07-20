# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""``axi ext quickstart <name>`` — one-shot author-first-time path.

Runs ``init`` + ``lint`` + ``validate --skip-tests`` + ``scan`` back to back
so an author types one command and ends up with a lint-, validate-, and
scan-green scaffold ready to publish. ``--publish`` composes ``publish --yes``
at the end for the full init-to-registered chain.

Design intent: mirror what a seasoned user would do by hand. Failures stop
the chain immediately with a clear pointer at the failing verb — the user
fixes the specific issue and re-runs.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from axiom.cli.ext._output import console, error, next_steps, status
from axiom.cli.ext.commands.init import InitProvider
from axiom.cli.ext.commands.lint import lint_extension
from axiom.cli.ext.commands.publish import publish_extension
from axiom.cli.ext.commands.scan import scan_extension
from axiom.cli.ext.commands.validate import validate_extension
from axiom.cli.ext.provider import CliContext


def _run_init(
    name: str,
    *,
    target_dir: Path | None,
    owner: str,
    license: str,
    description: str,
    template: str | None,
    context: CliContext,
) -> tuple[int, Path | None]:
    """Invoke the init provider in-process. Returns (rc, ext_path)."""
    provider = InitProvider()
    parser = argparse.ArgumentParser()
    provider.add_arguments(parser)
    argv: list[str] = [name]
    if target_dir is not None:
        argv.extend(["--dir", str(target_dir)])
    argv.extend(["--owner", owner])
    argv.extend(["--license", license])
    if description:
        argv.extend(["--description", description])
    if template:
        argv.extend(["--template", template])
    args = parser.parse_args(argv)
    rc = provider.run(args, context)
    if rc != 0:
        return rc, None
    base = target_dir if target_dir is not None else context.cwd
    return 0, base / name


class QuickstartProvider:
    """Built-in provider for ``axi ext quickstart <name>``."""

    verb = "quickstart"
    description = "One-shot init + lint + validate + scan (+ optional publish)"

    def add_arguments(self, parser: argparse.ArgumentParser) -> None:
        parser.add_argument("name", help="Extension name (purpose-named, no type suffix)")
        parser.add_argument(
            "--dir",
            dest="target_dir",
            default=None,
            help="Parent directory for the new extension (default: cwd)",
        )
        parser.add_argument(
            "--owner",
            default="b-tree-labs",
            help="Owning organization for the manifest",
        )
        parser.add_argument(
            "--license",
            default="Apache-2.0",
            help="SPDX license identifier (default: Apache-2.0)",
        )
        parser.add_argument(
            "--description",
            default="",
            help="One-line description (default: purpose-based)",
        )
        parser.add_argument(
            "--template",
            default=None,
            help="Template id (default: registry default)",
        )
        parser.add_argument(
            "--publish",
            action="store_true",
            help="Compose `axi ext publish --yes` as a final step",
        )
        parser.add_argument(
            "--yes",
            action="store_true",
            help="Alias for --publish (historical); matches the publish verb flag",
        )

    def run(self, args: argparse.Namespace, context: CliContext) -> int:
        con = console()
        target_dir = Path(args.target_dir).resolve() if args.target_dir else None

        # --- init ---------------------------------------------------------
        rc, ext_path = _run_init(
            args.name,
            target_dir=target_dir,
            owner=args.owner,
            license=args.license,
            description=args.description,
            template=args.template,
            context=context,
        )
        if rc != 0 or ext_path is None:
            error(
                "axi ext quickstart: init failed; see message above.",
                hint="fix the reported issue, then re-run quickstart",
            )
            return rc or 1
        con.print("")

        # --- lint ---------------------------------------------------------
        con.print(f"axi ext quickstart: linting {ext_path.name}")
        lint_findings = lint_extension(ext_path)
        lint_errors = [f for f in lint_findings if f.severity == "error"]
        if lint_errors:
            for f in lint_errors:
                status("fail", f.code, f.message)
            error(
                f"axi ext quickstart: lint found {len(lint_errors)} error(s)",
                hint=f"run `axi ext lint {ext_path}` for full details",
            )
            return 1
        status("pass", "lint", "no errors")

        # --- validate (skip tests to keep the chain fast) -----------------
        con.print(f"axi ext quickstart: validating {ext_path.name}")
        val_results = validate_extension(ext_path)
        val_failures = [r for r in val_results if not r.ok]
        if val_failures:
            for r in val_failures:
                status("fail", r.check, r.detail)
            error(
                f"axi ext quickstart: validate found {len(val_failures)} failure(s)",
                hint=f"run `axi ext validate {ext_path}` for the full report",
            )
            return 1
        status("pass", "validate", f"{len(val_results)} checks passed")

        # --- scan ---------------------------------------------------------
        con.print(f"axi ext quickstart: scanning {ext_path.name}")
        scan_result = scan_extension(ext_path)
        if scan_result.hard_failure:
            for c in scan_result.checks:
                if c.severity == "fail":
                    status("fail", c.check, c.detail)
            error(
                "axi ext quickstart: scan found hard failure(s)",
                hint=f"run `axi ext scan {ext_path}` for details",
            )
            return 1
        status("pass", "scan", "no hard failures")

        # --- publish (optional) -------------------------------------------
        if args.publish or args.yes:
            con.print(f"axi ext quickstart: publishing {ext_path.name}")
            try:
                publish_extension(
                    ext_path,
                    yes=True,
                    skip_tag_check=True,
                )
            except Exception as exc:  # noqa: BLE001
                error(
                    f"axi ext quickstart: publish failed: {exc}",
                    hint=f"run `axi ext publish {ext_path} --yes` once fixed",
                )
                return 1
            status("pass", "publish", "registered locally")
            con.print("")
            next_steps(
                [
                    f"axi ext install {args.name}    # Install the just-published version",
                    "axi ext list                       # Confirm registry + install state",
                ]
            )
            return 0

        # --- closing guidance ---------------------------------------------
        con.print("")
        con.print(
            f"Scaffolded and clean at {ext_path} — you're all green."
        )
        next_steps(
            [
                f"axi ext publish {args.name} --yes   # Sign and register locally",
                f"cd {ext_path}                       # Start implementing",
            ],
            header="To publish:",
        )
        return 0


__all__ = ["QuickstartProvider"]
