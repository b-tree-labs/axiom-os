# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""RIVET PR-scoped CI watcher (issue: PR-CI watch gap, 2026-05-19).

Fills a real gap surfaced when PR #211's Build Wheel job failed on a
GitHub Actions billing block and RIVET didn't notice because
`ci_monitor.check_pipelines` only polls the top-level latest workflow
run per repo (no PR scoping, no per-job breakdown, no failure
classification).

What this module does on each RIVET heartbeat tick:

  1. Enumerate the user's open PRs (`gh pr list --author @me`)
  2. Fetch per-job checks per PR (`gh pr checks <n> --json ...`)
  3. Classify failing checks: code | infra | flake | unknown
  4. Compare against last-seen state under
     ``~/.axi/agents/rivet/pr-checks.json``
  5. Return `StateFlip` events on transitions for the heartbeat / AXI
     layer to surface — failing → passing recoveries included

Classification matters because infra failures (billing, runner
unavailable, queue limit) need *operator action*, not a code fix —
the eventual responder layer routes them differently.

Today this is the polling skill (Layer 1 of the PR-CI watcher
design). GitHub App webhooks (Layer 2) and auto-responder (Layer 3)
build on top.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

# ---------------------------------------------------------------------------
# Data shapes
# ---------------------------------------------------------------------------


FailureClass = Literal["code", "infra", "flake", "unknown"]
OverallState = Literal["passing", "failing", "pending", "unknown"]


@dataclass(frozen=True)
class CheckRow:
    """One row from `gh pr checks <n> --json ...`."""

    name: str
    bucket: str  # "pass" | "fail" | "pending" | "cancel" | "skipping"
    state: str
    description: str
    link: str

    @property
    def is_failing(self) -> bool:
        return self.bucket == "fail"


@dataclass
class PRRef:
    """Minimal PR descriptor returned by `gh pr list`."""

    number: int
    title: str
    url: str
    head_branch: str


@dataclass
class PRChecks:
    """A PR plus the current state of all its checks."""

    pr_number: int
    title: str
    url: str
    head_branch: str
    rows: list[CheckRow] = field(default_factory=list)

    @property
    def overall(self) -> OverallState:
        if any(r.is_failing for r in self.rows):
            return "failing"
        if any(r.bucket == "pending" for r in self.rows):
            return "pending"
        return "passing"


@dataclass(frozen=True)
class StateFlip:
    """A PR's overall state changed since the last poll."""

    pr_number: int
    title: str
    url: str
    head_branch: str
    from_state: OverallState  # last seen
    to_state: OverallState     # current
    failing_rows: list[CheckRow] = field(default_factory=list)
    classification: FailureClass = "unknown"


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------


# Phrases that strongly suggest an infrastructure (billing / runner / queue
# / storage) failure rather than a code regression. Keep the list short and
# accurate — a false-positive here would route a real code failure away from
# a fix-branch responder. Add new phrases only after seeing them in the wild.
_INFRA_PATTERNS = (
    "spending limit",
    "billing",
    "payment",
    "github actions usage",
    "runner unavailable",
    "could not start",
    "could not start runner",
    "queue limit exceeded",
    "account payments",
    # Added 2026-06-01 per a consumer-repo storage-quota incident.
    # Provider returns these exact substrings when artifact storage is
    # exhausted. Auto-routes to TIDY's ``artifact_cleanup`` skill.
    "storage quota",
    "failed to createartifact",
    "artifact storage",
)


def classify_failure(row: CheckRow, log_excerpt: str = "") -> FailureClass:
    """Classify a failing check.

    Priority order:

    1. ``flake`` — test-pollution patterns (parallel-worker tmp_path race);
       routed to a rerun-without-xdist reproduction step.
    2. ``infra`` — billing / runner / queue / storage; routed to the
       operator (or to TIDY's ``artifact_cleanup`` for storage quota).
    3. ``code`` — default; routed to a fix-branch responder.
    """
    haystack = f"{row.description} {log_excerpt}".lower()
    # Test-pollution check first — these failures look code-like (assertion
    # errors, FileNotFoundError) but are infra-shape (parallel-worker race).
    if "popen-gw" in haystack and "pytest-of-" in haystack:
        return "flake"
    if any(p in haystack for p in _INFRA_PATTERNS):
        return "infra"
    return "code"


# ---------------------------------------------------------------------------
# `gh` invocation seam (replaceable in tests)
# ---------------------------------------------------------------------------


def _run_gh(args: list[str]) -> str:
    """Invoke `gh <args>` and return stdout. Empty string on failure.

    Test override: monkeypatch `pr_check_watcher._run_gh` to inject
    scripted JSON responses. The real impl never raises — gh CLI
    failures are treated as "no data" so a missing `gh` doesn't break
    the rest of the heartbeat.
    """
    try:
        result = subprocess.run(
            ["gh", *args],
            capture_output=True, text=True, timeout=15, check=False,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return ""
    if result.returncode != 0:
        return ""
    return result.stdout


# ---------------------------------------------------------------------------
# PR enumeration + check fetch
# ---------------------------------------------------------------------------


def list_user_prs() -> list[PRRef]:
    """List the current user's open PRs in the local repo."""
    out = _run_gh([
        "pr", "list", "--author", "@me",
        "--json", "number,title,url,headRefName",
    ])
    if not out.strip():
        return []
    try:
        data = json.loads(out)
    except json.JSONDecodeError:
        return []
    return [
        PRRef(
            number=int(d["number"]),
            title=d.get("title", ""),
            url=d.get("url", ""),
            head_branch=d.get("headRefName", ""),
        )
        for d in data
    ]


def fetch_pr_checks(
    *,
    pr_number: int,
    title: str,
    url: str,
    head_branch: str,
) -> PRChecks:
    """Fetch per-job check state for one PR."""
    out = _run_gh([
        "pr", "checks", str(pr_number),
        "--json", "name,bucket,state,description,link",
    ])
    rows: list[CheckRow] = []
    if out.strip():
        try:
            data = json.loads(out)
            for d in data:
                rows.append(CheckRow(
                    name=d.get("name", ""),
                    bucket=d.get("bucket", "unknown"),
                    state=d.get("state", "unknown"),
                    description=d.get("description", ""),
                    link=d.get("link", ""),
                ))
        except json.JSONDecodeError:
            pass
    return PRChecks(
        pr_number=pr_number, title=title, url=url, head_branch=head_branch,
        rows=rows,
    )


# ---------------------------------------------------------------------------
# State-flip detection
# ---------------------------------------------------------------------------


def _load_last_seen(path: Path) -> dict[str, dict]:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _save_last_seen(path: Path, state: dict[str, dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
    tmp.replace(path)


def detect_state_flips(
    current: list[PRChecks],
    last_seen_path: Path,
) -> list[StateFlip]:
    """Compare each PR's current overall state to the last-seen state.

    Returns one `StateFlip` per terminal-state transition (passing ↔
    failing). Pending → anything and anything → pending is NOT a flip
    — pending is mid-flight and would spam notifications on every
    poll. Persists the new state back to `last_seen_path` so the next
    tick can compare against it.

    First-time-seen PRs that are currently failing also emit a flip
    (from_state="unknown") so the operator sees them; first-time
    passing PRs do not (no signal worth surfacing).
    """
    last_seen = _load_last_seen(last_seen_path)
    flips: list[StateFlip] = []
    new_state: dict[str, dict] = {}

    for pr in current:
        key = str(pr.pr_number)
        current_overall = pr.overall
        new_state[key] = {"overall": current_overall}

        if current_overall == "pending":
            # Carry forward whatever we'd previously persisted so we don't
            # forget a pre-existing failing/passing state during an in-flight
            # rerun. Skip flip detection while pending.
            if key in last_seen:
                new_state[key] = last_seen[key]
            continue

        prior_overall: OverallState = last_seen.get(key, {}).get("overall", "unknown")
        if prior_overall == current_overall:
            continue
        if prior_overall == "unknown" and current_overall == "passing":
            # First sighting + already green → no signal worth surfacing.
            continue

        failing_rows = [r for r in pr.rows if r.is_failing]
        classification: FailureClass = "unknown"
        if failing_rows:
            # Take the dominant classification: infra wins over code
            # because mixed-infra-and-code failures should route to the
            # operator first (the infra block likely caused cascade).
            classifications = {classify_failure(r) for r in failing_rows}
            if "infra" in classifications:
                classification = "infra"
            elif "code" in classifications:
                classification = "code"
            else:
                classification = "unknown"

        flips.append(StateFlip(
            pr_number=pr.pr_number,
            title=pr.title,
            url=pr.url,
            head_branch=pr.head_branch,
            from_state=prior_overall,
            to_state=current_overall,
            failing_rows=failing_rows,
            classification=classification,
        ))

    _save_last_seen(last_seen_path, new_state)
    return flips


# ---------------------------------------------------------------------------
# Top-level entry — called from RIVET heartbeat
# ---------------------------------------------------------------------------


def watch_user_prs(*, state_dir: Path) -> list[StateFlip]:
    """One sweep: list PRs, fetch checks, detect flips, persist.

    `state_dir` is the agent's state directory (`~/.axi/`); the watcher
    persists under `<state_dir>/agents/rivet/pr-checks.json`. Returns
    the state flips the heartbeat caller can surface to the operator
    (via stdout or the future notification path).
    """
    prs = list_user_prs()
    if not prs:
        return []
    current = [
        fetch_pr_checks(
            pr_number=p.number, title=p.title, url=p.url,
            head_branch=p.head_branch,
        )
        for p in prs
    ]
    state_path = state_dir / "agents" / "rivet" / "pr-checks.json"
    return detect_state_flips(current, state_path)
