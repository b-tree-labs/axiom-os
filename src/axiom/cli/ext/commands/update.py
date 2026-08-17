# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""``axi ext update [<name>]`` — refresh installed extensions to latest.

Without ``<name>``, every axi-installed extension is considered. With a
name, only that one. For each target we compare the installed version to
the registry's ``latest`` and plan an update when the registry is
strictly newer.

The update itself is ``uninstall -> install @ new_version``. Executed
sequentially; on first failure we stop and report which targets updated
cleanly vs which didn't (partial progress is surfaced, not hidden).
"""

from __future__ import annotations

import argparse
import os
from dataclasses import dataclass

from axiom.cli.ext._output import console
from axiom.cli.ext.commands.install import install_extension
from axiom.cli.ext.commands.uninstall import uninstall_extension
from axiom.cli.ext.install_state import InstallRecord, list_installed
from axiom.cli.ext.provider import CliContext
from axiom.cli.ext.registry_backend import read_index


@dataclass(frozen=True)
class UpdatePlan:
    """Single planned update."""

    name: str
    old_version: str
    new_version: str


@dataclass(frozen=True)
class UpdateOutcome:
    """End-of-run report of which updates succeeded and which didn't."""

    updated: list[UpdatePlan]
    skipped: list[InstallRecord]
    failed: list[tuple[UpdatePlan, str]]


def _registry_latest(name: str) -> str | None:
    idx = read_index()
    entry = (idx.get("extensions") or {}).get(name) or {}
    latest = entry.get("latest")
    return str(latest) if latest else None


def plan_updates(target_names: list[str] | None = None) -> list[UpdatePlan]:
    """Return the list of updates that would apply."""
    installed = list_installed()
    if target_names is not None:
        wanted = set(target_names)
        installed = [r for r in installed if r.name in wanted]
        # Remember: a missing target is an error the caller surfaces.
        known = {r.name for r in installed}
        for name in target_names:
            if name not in known:
                raise RuntimeError(
                    f"{name} is not installed; run `axi ext list` to see "
                    "what is."
                )

    plans: list[UpdatePlan] = []
    for rec in installed:
        latest = _registry_latest(rec.name)
        if latest is None:
            continue  # no registry entry — nothing to update to
        if latest != rec.version:
            plans.append(
                UpdatePlan(
                    name=rec.name,
                    old_version=rec.version,
                    new_version=latest,
                )
            )
    return plans


def update_extensions(
    target_names: list[str] | None = None,
    *,
    no_pip: bool = False,
    dry_run: bool = False,
    announce=None,
) -> UpdateOutcome:
    """Execute the planned updates sequentially.

    Returns an :class:`UpdateOutcome`. If ``dry_run`` is set, the plans
    are returned in ``updated`` without being executed.
    """
    announce = announce or (lambda msg: None)
    plans = plan_updates(target_names)

    # "skipped" is the installed-but-already-latest set — shown to the
    # user so they know nothing needed doing.
    skipped: list[InstallRecord] = []
    plan_names = {p.name for p in plans}
    all_installed = list_installed()
    if target_names is not None:
        wanted = set(target_names)
        all_installed = [r for r in all_installed if r.name in wanted]
    for rec in all_installed:
        if rec.name not in plan_names:
            skipped.append(rec)

    if dry_run:
        return UpdateOutcome(updated=list(plans), skipped=skipped, failed=[])

    updated: list[UpdatePlan] = []
    failed: list[tuple[UpdatePlan, str]] = []

    for plan in plans:
        try:
            announce(
                f"Updating {plan.name}: {plan.old_version} -> "
                f"{plan.new_version}"
            )
            uninstall_extension(plan.name, no_pip=no_pip, announce=announce)
            install_extension(
                plan.name,
                version=plan.new_version,
                no_pip=no_pip,
                announce=announce,
            )
            updated.append(plan)
        except RuntimeError as exc:
            failed.append((plan, str(exc)))
            # Stop on first failure — pre-declared policy so the user can
            # investigate before later updates mutate more state.
            break

    return UpdateOutcome(updated=updated, skipped=skipped, failed=failed)


class UpdateProvider:
    """Built-in provider for ``axi ext update [<name>]``."""

    verb = "update"
    description = "Update installed extensions to the registry's latest"

    def add_arguments(self, parser: argparse.ArgumentParser) -> None:
        parser.add_argument(
            "name",
            nargs="?",
            default=None,
            help="Extension to update (default: every axi-installed extension)",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Print the plan; don't execute",
        )
        parser.add_argument(
            "--registry",
            dest="registry_override",
            default=None,
            help="Override the registry URL (file:// only at v0.1)",
        )
        parser.add_argument(
            "--no-pip",
            action="store_true",
            help=argparse.SUPPRESS,
        )

    def run(self, args: argparse.Namespace, context: CliContext) -> int:
        if args.registry_override is not None:
            if not args.registry_override.startswith("file://"):
                print(
                    f"axi ext update: --registry must use the file:// scheme "
                    f"(got {args.registry_override!r})."
                )
                return 1
            os.environ["AXIOM_REGISTRY_URL"] = args.registry_override

        targets: list[str] | None = [args.name] if args.name else None

        def _announce(msg: str) -> None:
            print(msg)

        try:
            outcome = update_extensions(
                targets,
                no_pip=args.no_pip,
                dry_run=args.dry_run,
                announce=_announce,
            )
        except RuntimeError as exc:
            print(f"axi ext update: {exc}")
            return 1

        con = console()
        if args.dry_run:
            if not outcome.updated:
                con.print("all extensions up to date")
                return 0
            con.print("dry-run plan:")
            for p in outcome.updated:
                con.print(f"  {p.name}: {p.old_version} -> {p.new_version}")
            return 0

        if not outcome.updated and not outcome.failed:
            con.print("all extensions up to date")
            return 0

        for p in outcome.updated:
            con.print(f"Updated {p.name}: {p.old_version} -> {p.new_version}")

        if outcome.failed:
            for p, msg in outcome.failed:
                con.print(f"FAILED {p.name}: {msg}")
            remaining = len(
                [
                    p
                    for p in plan_updates(targets)
                    if p.name not in {u.name for u in outcome.updated}
                    and p.name not in {fp.name for fp, _ in outcome.failed}
                ]
            )
            if remaining:
                con.print(
                    f"{remaining} target(s) not attempted — re-run after "
                    "fixing the failure above."
                )
            return 1

        return 0


__all__ = [
    "UpdateOutcome",
    "UpdatePlan",
    "UpdateProvider",
    "plan_updates",
    "update_extensions",
]
