# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""axi review — run REV-U multi-pass review against the local git diff.

Usage:
    axi review [--base <ref>] [--severity <level>] [--pass <kind>] [--json] [--no-validator]

Exit codes:
    0  no blocker findings
    1  one or more blocker findings found
"""

from __future__ import annotations

import sys
import time

import click

from axiom.extensions.builtins.review.agents.rev_u.agent import RevUAgent
from axiom.extensions.builtins.review.tools.diff import local_diff
from axiom.extensions.builtins.review.tools.findings import (
    SEVERITY_ORDER,
    Finding,
    FindingSet,
)

_SEVERITY_COLORS = {
    "blocker": "red",
    "major": "yellow",
    "minor": "cyan",
    "nit": "bright_black",
}


def _severity_rank(s: str) -> int:
    try:
        return SEVERITY_ORDER.index(s)
    except ValueError:
        return 99


def _render_terminal(fset: FindingSet, min_severity: str) -> None:
    """Print findings to stdout grouped by severity → pass_kind → file."""
    min_rank = _severity_rank(min_severity)
    visible = [f for f in fset if _severity_rank(f.severity) <= min_rank]

    if not visible:
        click.echo("No findings to report.")
        return

    grouped: dict[str, dict[str, list[Finding]]] = {}
    for sev in SEVERITY_ORDER:
        for f in visible:
            if f.severity != sev:
                continue
            grouped.setdefault(sev, {}).setdefault(f.pass_kind, []).append(f)

    for sev in SEVERITY_ORDER:
        if sev not in grouped:
            continue
        color = _SEVERITY_COLORS.get(sev, "white")
        click.echo(click.style(f"\n[{sev.upper()}]", fg=color, bold=True))
        for pass_kind, findings in sorted(grouped[sev].items()):
            click.echo(f"  {pass_kind}")
            for f in findings:
                loc = f"{f.path}:{f.line}" if f.line else f.path
                click.echo(f"    {loc}  {f.message}")
                if f.suggested_fix:
                    click.echo(f"      → {f.suggested_fix}")


def _render_footer(fset: FindingSet, elapsed: float, num_files: int) -> None:
    counts = {s: 0 for s in SEVERITY_ORDER}
    for f in fset:
        counts[f.severity] = counts.get(f.severity, 0) + 1

    total = sum(counts.values())
    parts = []
    abbrev = {"blocker": "B", "major": "M", "minor": "m", "nit": "n"}
    for sev in SEVERITY_ORDER:
        if counts[sev]:
            parts.append(f"{counts[sev]}{abbrev[sev]}")

    summary = " · ".join(parts) if parts else "0 findings"
    click.echo(
        f"\n{total} findings ({summary}) across {num_files} files · ~{elapsed:.1f}s"
    )


@click.command("review")
@click.option("--base", default="main", show_default=True, help="Git ref to diff against")
@click.option(
    "--severity",
    default="minor",
    show_default=True,
    type=click.Choice(SEVERITY_ORDER),
    help="Minimum severity level to display",
)
@click.option(
    "--pass",
    "passes",
    multiple=True,
    type=click.Choice(["correctness", "performance", "security", "docs", "tests"]),
    help="Run only these pass kinds (repeatable; default: all 5)",
)
@click.option("--json", "json_output", is_flag=True, help="Emit JSON to stdout")
@click.option("--no-validator", "no_validator", is_flag=True, help="Skip validator stage")
def main(
    base: str,
    severity: str,
    passes: tuple[str, ...],
    json_output: bool,
    no_validator: bool,
) -> None:
    """Run REV-U multi-pass review against the local git diff."""
    t0 = time.monotonic()

    diff = local_diff(base=base)
    if not diff:
        click.echo("No diff found. Nothing to review.")
        sys.exit(0)

    selected_passes = list(passes) if passes else None

    agent = RevUAgent(repo_root=".")
    fset = agent.review(diff, passes=selected_passes, run_validator=not no_validator)

    elapsed = time.monotonic() - t0

    if json_output:
        click.echo(fset.to_json())
    else:
        _render_terminal(fset, min_severity=severity)
        touched_files = _count_touched_files(diff)
        _render_footer(fset, elapsed, touched_files)

    # Exit 1 if any blocker findings exist (regardless of --severity filter).
    has_blockers = any(f.severity == "blocker" for f in fset)
    sys.exit(1 if has_blockers else 0)


def _count_touched_files(diff: str) -> int:
    import re

    return len(re.findall(r"^\+\+\+ b/", diff, re.MULTILINE))


__all__ = ["main"]
