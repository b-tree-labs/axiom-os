# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Was this commit's exact tree already tested, whole, on a queue branch? (#2399, slice 3).

A push to main whose tree equals the tree of a queue-branch run that completed green on
the FULL path adds nothing a second run could find, so CI skips its heavy jobs. The match
is by tree, never by sha: a fast-forward keeps the sha, a merge commit does not, and both
carry the tree that was tested.

Every doubt fails closed (the suite runs): an unreadable run list, a run that is not a
push to a queue branch, a run that did not conclude green, a run whose heavy jobs were
skipped (a narrowed run, not the full path), or a job list that cannot be read.

    python -m axiom.extensions.builtins.release.queue_tree --repo OWNER/NAME --sha SHA

prints ``skip=true|false`` and a ``reason=`` line. Reads only (``gh api`` GETs).
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass

QUEUE_BRANCH_PREFIX = "rivet-queue/"

#: Jobs a queue run must have run to a success, as named in ci.yml. A name with a matrix
#: suffix, such as ``Unit Tests (3.13)``, matches by prefix. A run on which any of these
#: was skipped did not take the full path.
FULL_PATH_JOBS = (
    "Lint",
    "Smoke",
    "Unit Shard",
    "Unit Tests",
    "Integration Tests",
    "Migration Tests",
    "Install-Path Tests",
)

Getter = Callable[[str], object]


@dataclass(frozen=True)
class Judgement:
    skip: bool
    reason: str
    run_id: int = 0


def tree_of(sha: str, *, cwd: str | None = None) -> str:
    """The tree id of ``sha`` in the repository at ``cwd``. Empty when unreadable."""
    try:
        out = subprocess.run(
            ["git", "rev-parse", f"{sha}^{{tree}}"],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=30,
            check=True,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return out.stdout.strip()


def _full_path(jobs: object) -> str:
    """Empty when every full-path job succeeded; else the reason it was not the full path."""
    if not isinstance(jobs, list) or not jobs:
        return "jobs_unreadable"
    for needed in FULL_PATH_JOBS:
        mine = [j for j in jobs if str(j.get("name", "")).startswith(needed)]
        if not mine:
            return f"job_missing: {needed}"
        if any(j.get("conclusion") != "success" for j in mine):
            return f"job_not_success: {needed}"
    return ""


def judge(tree: str, runs: object, jobs_for: Callable[[int], object]) -> Judgement:
    """Skip only when a green full-path queue run has this tree. Fail closed otherwise."""
    if not tree:
        return Judgement(False, "tree_unknown")
    if not isinstance(runs, list):
        return Judgement(False, "runs_unreadable")
    saw_tree = ""
    for run in runs:
        if not isinstance(run, dict):
            continue
        if (run.get("head_commit") or {}).get("tree_id") != tree:
            continue
        if run.get("event") != "push" or not str(run.get("head_branch", "")).startswith(
            QUEUE_BRANCH_PREFIX
        ):
            saw_tree = saw_tree or "not_a_queue_run"
            continue
        if run.get("status") != "completed" or run.get("conclusion") != "success":
            saw_tree = "queue_run_not_green"
            continue
        try:
            run_id = int(run["id"])
            why = _full_path(jobs_for(run_id))
        except (KeyError, TypeError, ValueError, OSError, subprocess.SubprocessError):
            saw_tree = "jobs_unreadable"
            continue
        if why:
            saw_tree = why
            continue
        return Judgement(True, "tree_tested_on_queue_branch", run_id)
    return Judgement(False, saw_tree or "no_queue_run_for_tree")


def _gh(path: str) -> object:
    out = subprocess.run(
        ["gh", "api", path], capture_output=True, text=True, timeout=120, check=True
    )
    return json.loads(out.stdout or "null")


def main(argv: list[str] | None = None, *, get: Getter | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--repo", required=True)
    ap.add_argument("--sha", required=True)
    ap.add_argument("--tree", default="")
    args = ap.parse_args(argv)
    read = get or _gh
    tree = args.tree or tree_of(args.sha)
    try:
        listing = read(
            f"repos/{args.repo}/actions/workflows/ci.yml/runs?event=push&status=success&per_page=100"
        )
        runs = listing.get("workflow_runs") if isinstance(listing, dict) else None
    except (OSError, subprocess.SubprocessError, ValueError):
        runs = None

    def jobs_for(run_id: int) -> object:
        data = read(f"repos/{args.repo}/actions/runs/{run_id}/jobs?per_page=100")
        return data.get("jobs") if isinstance(data, dict) else None

    verdict = judge(tree, runs, jobs_for)
    print(f"skip={'true' if verdict.skip else 'false'}")
    print(f"reason={verdict.reason}")
    if verdict.run_id:
        print(f"queue_run={verdict.run_id}")
    print(f"tree={tree}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
