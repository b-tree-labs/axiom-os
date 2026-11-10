# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Cloud-routine watcher — RIVET's lifecycle eye on remote agents.

A scheduled `RemoteTrigger` (a CCR cloud routine) eventually pushes a
branch and opens a PR. This module lets RIVET persist a list of such
routines and detect their lifecycle transitions across sessions:
`pending` → `branch_seen` → `pr_opened` → `completed`.

We do not poll the RemoteTrigger API directly (auth is OAuth-bound).
We poll the *output* via `gh` — branch existence, then PR-by-head — so
the watcher works wherever `gh auth` is wired up.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path

DEFAULT_REPO = "b-tree-labs/axiom-os"

_STATES = ("pending", "branch_seen", "pr_opened", "completed")


@dataclass
class TrackedRoutine:
    trigger_id: str
    branch: str
    repo: str = DEFAULT_REPO
    note: str = ""
    state: str = "pending"
    pr_number: int | None = None
    registered_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
    last_checked: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


def _tracked_path(state_dir: Path) -> Path:
    return state_dir / "agents" / "rivet" / "tracked_routines.json"


def load_tracked(state_dir: Path) -> list[TrackedRoutine]:
    path = _tracked_path(state_dir)
    if not path.exists():
        return []
    raw = json.loads(path.read_text(encoding="utf-8") or "[]")
    return [TrackedRoutine(**r) for r in raw]


def save_tracked(state_dir: Path, routines: list[TrackedRoutine]) -> None:
    path = _tracked_path(state_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps([r.to_dict() for r in routines], indent=2) + "\n",
        encoding="utf-8",
    )


def track(
    state_dir: Path,
    trigger_id: str,
    branch: str,
    repo: str = DEFAULT_REPO,
    note: str = "",
) -> TrackedRoutine:
    routines = load_tracked(state_dir)
    for r in routines:
        if r.trigger_id == trigger_id:
            return r
    new = TrackedRoutine(trigger_id=trigger_id, branch=branch, repo=repo, note=note)
    routines.append(new)
    save_tracked(state_dir, routines)
    return new


def untrack(state_dir: Path, trigger_id: str) -> bool:
    routines = load_tracked(state_dir)
    kept = [r for r in routines if r.trigger_id != trigger_id]
    if len(kept) == len(routines):
        return False
    save_tracked(state_dir, kept)
    return True


def _gh_branch_exists(repo: str, branch: str) -> bool:
    try:
        result = subprocess.run(
            ["gh", "api", f"repos/{repo}/branches/{branch}"],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        return result.returncode == 0
    except Exception:
        return False


def _gh_pr_for_branch(repo: str, branch: str) -> dict | None:
    try:
        result = subprocess.run(
            [
                "gh",
                "pr",
                "list",
                "--repo",
                repo,
                "--head",
                branch,
                "--state",
                "all",
                "--json",
                "number,title,state,isDraft,url",
                "--limit",
                "1",
            ],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        if result.returncode != 0:
            return None
        prs = json.loads(result.stdout or "[]")
        return prs[0] if prs else None
    except Exception:
        return None


def poll_routines(state_dir: Path) -> list[dict]:
    """Check each tracked routine and emit any state transitions.

    Returns a list of transition entries (the same shape that gets appended
    to `heartbeat.jsonl` so downstream consumers see them).
    """
    routines = load_tracked(state_dir)
    transitions: list[dict] = []
    now = datetime.now(UTC).isoformat()
    changed = False

    for r in routines:
        prior = r.state
        r.last_checked = now

        if r.state == "completed":
            continue

        if r.state == "pending":
            if _gh_branch_exists(r.repo, r.branch):
                r.state = "branch_seen"

        if r.state in ("branch_seen", "pending"):
            pr = _gh_pr_for_branch(r.repo, r.branch)
            if pr:
                r.state = "pr_opened"
                r.pr_number = int(pr["number"])
                if pr.get("state") in ("MERGED", "CLOSED"):
                    r.state = "completed"

        if r.state != prior:
            changed = True
            transitions.append(
                {
                    "kind": "routine_transition",
                    "trigger_id": r.trigger_id,
                    "branch": r.branch,
                    "repo": r.repo,
                    "from": prior,
                    "to": r.state,
                    "pr_number": r.pr_number,
                    "ts": now,
                }
            )

    if changed:
        save_tracked(state_dir, routines)

    return transitions
