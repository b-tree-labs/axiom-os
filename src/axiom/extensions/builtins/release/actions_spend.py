# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""What RIVET says about GitHub Actions before the budget is gone.

A red check that says "usage limit" is too late. This reads minutes already
used, runs already going, and how many pull requests would each start another
suite, and returns one sentence when the next push would spend money we do
not have.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

#: One pull request's unit suite, in billed minutes. The 3.13 leg is about
#: twenty. Used when the billing API does not name a per-job cost.
SUITE_MINUTES = 20


@dataclass(frozen=True)
class SpendReading:
    minutes_used: int | None
    included_minutes: int | None
    in_progress: int
    open_prs: int
    #: Learned minutes for one suite in this repo. None keeps ``SUITE_MINUTES``.
    suite_minutes: int | None = None


def learn_suite_minutes(samples: Iterable[int | float]) -> int:
    """Median positive sample, in minutes. No samples keeps ``SUITE_MINUTES``."""
    positive = sorted(
        int(sample)
        for sample in samples
        if isinstance(sample, (int, float)) and not isinstance(sample, bool) and sample > 0
    )
    if not positive:
        return SUITE_MINUTES
    middle = len(positive) // 2
    if len(positive) % 2:
        return positive[middle]
    return int(round((positive[middle - 1] + positive[middle]) / 2))


def suite_cost(reading: SpendReading, override: int | None = None) -> int:
    """Per-suite minutes: an override, else a learned cost, else ``SUITE_MINUTES``."""
    if override is not None and override > 0:
        return int(override)
    learned = reading.suite_minutes
    if isinstance(learned, int) and learned > 0:
        return learned
    return SUITE_MINUTES


def assess(reading: SpendReading, *, suite_minutes: int | None = None) -> str | None:
    """One sentence RIVET should say, or None when there is nothing to say."""
    cost = suite_cost(reading, suite_minutes)
    left = _remaining(reading)
    queued = reading.in_progress * cost
    if left is not None and left <= 0:
        used = reading.minutes_used or 0
        included = reading.included_minutes or 0
        return (
            f"Actions budget is spent ({used} of {included} minutes). "
            "Do not open or update a pull request."
        )
    if left is not None and queued > left:
        return (
            f"{reading.in_progress} runs already in progress need about {queued} minutes "
            f"and {left} remain. Stop updating pull requests onto main."
        )
    projected = reading.open_prs * cost
    if left is not None and reading.open_prs > 1 and projected > left:
        return (
            f"{reading.open_prs} open pull requests would cost about {projected} minutes "
            f"and {left} remain. Fold the next change into one that is already open."
        )
    if left is None and reading.in_progress >= 4:
        return (
            f"{reading.in_progress} Actions runs are in progress. "
            "Wait for them before opening another pull request."
        )
    return None


def should_speak(message: str | None, last: str | None) -> bool:
    """Say it once. Repeat only when the sentence changes."""
    return bool(message) and message != last


def load_last(state_dir: Path) -> str | None:
    path = Path(state_dir) / "agents" / "rivet" / "spend.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    last = data.get("last") if isinstance(data, dict) else None
    return last if isinstance(last, str) else None


def save_last(state_dir: Path, message: str | None) -> None:
    path = Path(state_dir) / "agents" / "rivet" / "spend.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"last": message or ""}) + "\n", encoding="utf-8")


def watched_repos(state_dir: Path | None = None) -> tuple[str, ...]:
    """Repos whose Actions minutes a heartbeat watches.

    ``AXIOM_ACTIONS_REPOS`` (comma-separated) wins. Otherwise the lines in
    ``<state>/agents/rivet/actions-repos``. With neither, the checkout's own
    GitHub repo. The source does not name a consumer.
    """
    import os

    raw = os.environ.get("AXIOM_ACTIONS_REPOS", "").strip()
    if raw:
        return tuple(part.strip() for part in raw.split(",") if part.strip())
    if state_dir is not None:
        path = Path(state_dir) / "agents" / "rivet" / "actions-repos"
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            lines = []
        listed = tuple(
            line.strip() for line in lines if line.strip() and not line.strip().startswith("#")
        )
        if listed:
            return listed
    return _checkout_repo()


def _checkout_repo() -> tuple[str, ...]:
    import subprocess

    try:
        done = subprocess.run(
            ["gh", "repo", "view", "--json", "nameWithOwner", "-q", ".nameWithOwner"],
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ()
    name = done.stdout.strip()
    if done.returncode != 0 or not name or "\n" in name:
        return ()
    return (name,)


def fetch_reading(repos: tuple[str, ...]) -> SpendReading:
    """Minutes, in-progress runs, and open pull requests. Never raises."""
    in_progress = 0
    open_prs = 0
    for repo in repos:
        in_progress += _count_json(
            ["gh", "run", "list", "--repo", repo, "--limit", "30", "--json", "status"],
            lambda rows: sum(1 for row in rows if row.get("status") in ("in_progress", "queued", "waiting")),
        )
        open_prs += _count_json(
            ["gh", "pr", "list", "--repo", repo, "--state", "open", "--limit", "50", "--json", "number"],
            len,
        )
    used, included = _billing()
    return SpendReading(used, included, in_progress, open_prs)


def _count_json(cmd: list[str], reduce) -> int:
    import subprocess

    try:
        done = subprocess.run(cmd, capture_output=True, text=True, timeout=20, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return 0
    if done.returncode != 0 or not done.stdout.strip():
        return 0
    try:
        rows = json.loads(done.stdout)
    except json.JSONDecodeError:
        return 0
    if not isinstance(rows, list):
        return 0
    try:
        return int(reduce(rows))
    except (TypeError, ValueError):
        return 0


def _billing() -> tuple[int | None, int | None]:
    import subprocess

    for path in ("orgs/b-tree-labs/settings/billing/actions", "user/settings/billing/actions"):
        try:
            done = subprocess.run(
                ["gh", "api", path], capture_output=True, text=True, timeout=20, check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            continue
        if done.returncode != 0 or not done.stdout.strip():
            continue
        try:
            data = json.loads(done.stdout)
        except json.JSONDecodeError:
            continue
        if not isinstance(data, dict):
            continue
        used = data.get("total_minutes_used")
        included = data.get("included_minutes")
        if isinstance(used, (int, float)) and isinstance(included, (int, float)):
            return int(used), int(included)
    return None, None


def _remaining(reading: SpendReading) -> int | None:
    if reading.minutes_used is None or reading.included_minutes is None:
        return None
    return reading.included_minutes - reading.minutes_used
