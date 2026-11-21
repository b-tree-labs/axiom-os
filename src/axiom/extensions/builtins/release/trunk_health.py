# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""RIVET trunk + release-tag health watchers.

Closes two gaps surfaced by the 2026-06-01 audit: trunk on b-tree-labs/
axiom-os AND a consumer repo was red and nobody
saw, AND two release-tag publish runs (v0.27.0, v0.28.0) failed
without surfacing.

The poll-and-emit shape is shared with ``pr_check_watcher``: state file
persists last-seen status per ``(repo, ref)``, transitions emit
findings (red → red is silent; green → red is loud).

This module is provider-agnostic via the existing ``ci_monitor``
abstraction; GitHub today, GitLab next via the same Pipeline contract.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

log = logging.getLogger("axiom.release.trunk_health")


# ---------------------------------------------------------------------------
# Data shapes
# ---------------------------------------------------------------------------


TrunkSeverity = Literal["healthy", "red_first_tick", "red_persistent"]
TagSeverity = Literal["release_tag_red", "release_tag_recovery"]


@dataclass(frozen=True)
class TrunkSnapshot:
    """One observation of a (repo, ref) trunk's CI status."""

    repo: str
    ref: str
    status: str
    """``success`` / ``failure`` / ``pending`` / ``unknown``."""
    url: str
    """Link to the latest workflow run."""
    observed_at: datetime


@dataclass(frozen=True)
class TrunkFinding:
    """A red-trunk hygiene event surfaced to the operator."""

    repo: str
    ref: str
    severity: TrunkSeverity
    detail: str
    run_url: str
    first_seen_red_at: datetime | None


@dataclass(frozen=True)
class ReleaseTagFinding:
    """A release-tag CI failure event."""

    repo: str
    tag: str
    severity: TagSeverity
    detail: str
    run_url: str


# ---------------------------------------------------------------------------
# State persistence
# ---------------------------------------------------------------------------


def _state_path(state_dir: Path) -> Path:
    return state_dir / "agents" / "rivet" / "trunk-health.json"


def _release_tag_state_path(state_dir: Path) -> Path:
    return state_dir / "agents" / "rivet" / "release-tag-health.json"


def load_trunk_state(state_dir: Path) -> dict[str, dict]:
    """Return the persisted ``(repo, ref) → state`` map."""
    p = _state_path(state_dir)
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def save_trunk_state(state_dir: Path, state: dict[str, dict]) -> None:
    p = _state_path(state_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(state, indent=2, default=str))


def load_release_tag_state(state_dir: Path) -> dict[str, dict]:
    p = _release_tag_state_path(state_dir)
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def save_release_tag_state(state_dir: Path, state: dict[str, dict]) -> None:
    p = _release_tag_state_path(state_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(state, indent=2, default=str))


# ---------------------------------------------------------------------------
# Trunk health — first-tick + persistent severities
# ---------------------------------------------------------------------------


def assess_trunk(
    current: TrunkSnapshot,
    *,
    prior: dict | None,
    now: datetime | None = None,
) -> tuple[dict, TrunkFinding | None]:
    """Decide the new state + emit a finding when warranted.

    Transitions:
      - green → red: ``red_first_tick`` severity (loud, fresh)
      - red → red:   ``red_persistent`` ONLY if it's been red for more than
                     one tick (avoid repeating the same alert; surface for
                     escalation paths that escalate over time)
      - red → green: no finding (clearance — caller can choose to emit
                     a recovery event separately)
      - any → green: no finding
    """
    now = now or datetime.now(timezone.utc)
    key_state = {
        "status": current.status,
        "url": current.url,
        "last_observed_at": now.isoformat(),
    }

    is_red = current.status in ("failure", "failed")
    if not is_red:
        # If we just transitioned from red → green, record clearance.
        if prior and prior.get("status") in ("failure", "failed"):
            key_state["first_seen_red_at"] = None
        return key_state, None

    # Red state — track first-seen.
    first_seen_iso = prior.get("first_seen_red_at") if prior else None
    if not first_seen_iso:
        # Fresh failure → first-tick finding.
        key_state["first_seen_red_at"] = now.isoformat()
        return key_state, TrunkFinding(
            repo=current.repo,
            ref=current.ref,
            severity="red_first_tick",
            detail=(
                f"trunk {current.repo}/{current.ref} just went red "
                f"(was {prior.get('status', 'unknown') if prior else 'unobserved'})"
            ),
            run_url=current.url,
            first_seen_red_at=now,
        )

    # Already-red — preserve first-seen; emit persistent if more than one tick.
    key_state["first_seen_red_at"] = first_seen_iso
    first_seen = datetime.fromisoformat(first_seen_iso)
    # The "more than one tick" predicate uses the heartbeat interval as
    # implicit clock; if the caller is on PULSE schedule, this is the
    # right shape. Avoid spamming: only re-fire every 6 ticks (30 min
    # on the default 5-min heartbeat).
    silence_window_s = (now - first_seen).total_seconds()
    if silence_window_s and int(silence_window_s) % (5 * 60 * 6) < 60:
        return key_state, TrunkFinding(
            repo=current.repo,
            ref=current.ref,
            severity="red_persistent",
            detail=(
                f"trunk {current.repo}/{current.ref} has been red for "
                f"{silence_window_s/60:.0f} minutes"
            ),
            run_url=current.url,
            first_seen_red_at=first_seen,
        )
    return key_state, None


def process_trunk_snapshots(
    snapshots: list[TrunkSnapshot],
    *,
    state_dir: Path,
    now: datetime | None = None,
) -> list[TrunkFinding]:
    """Top-level: apply state machine + persist + return findings."""
    state = load_trunk_state(state_dir)
    findings: list[TrunkFinding] = []
    for snap in snapshots:
        key = f"{snap.repo}::{snap.ref}"
        new_state, finding = assess_trunk(snap, prior=state.get(key), now=now)
        state[key] = new_state
        if finding is not None:
            findings.append(finding)
    save_trunk_state(state_dir, state)
    return findings


# ---------------------------------------------------------------------------
# Release-tag health
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ReleaseTagSnapshot:
    repo: str
    tag: str
    status: str
    url: str


def assess_release_tag(
    current: ReleaseTagSnapshot, *, prior: dict | None
) -> tuple[dict, ReleaseTagFinding | None]:
    """Release-tag failures get a single loud event; recoveries surface too.

    The asymmetry vs trunk: a single failed release tag is *always* worth
    flagging — there's no "first tick vs persistent" gradient for tags;
    a tag publish either succeeds or it doesn't, and a recovery means
    a re-tag worked.
    """
    new_state = {"status": current.status, "url": current.url}
    prior_status = prior.get("status") if prior else None
    is_red = current.status in ("failure", "failed")
    if is_red and prior_status != current.status:
        return new_state, ReleaseTagFinding(
            repo=current.repo,
            tag=current.tag,
            severity="release_tag_red",
            detail=(
                f"release tag {current.tag} publish for {current.repo} "
                "failed — verify wheel was published; consider re-cutting"
            ),
            run_url=current.url,
        )
    if not is_red and prior_status in ("failure", "failed"):
        return new_state, ReleaseTagFinding(
            repo=current.repo,
            tag=current.tag,
            severity="release_tag_recovery",
            detail=f"release tag {current.tag} publish recovered",
            run_url=current.url,
        )
    return new_state, None


def process_release_tag_snapshots(
    snapshots: list[ReleaseTagSnapshot],
    *,
    state_dir: Path,
) -> list[ReleaseTagFinding]:
    state = load_release_tag_state(state_dir)
    findings: list[ReleaseTagFinding] = []
    for snap in snapshots:
        key = f"{snap.repo}::{snap.tag}"
        new_state, finding = assess_release_tag(snap, prior=state.get(key))
        state[key] = new_state
        if finding is not None:
            findings.append(finding)
    save_release_tag_state(state_dir, state)
    return findings


__all__ = [
    "ReleaseTagFinding",
    "ReleaseTagSnapshot",
    "TrunkFinding",
    "TrunkSnapshot",
    "assess_release_tag",
    "assess_trunk",
    "process_release_tag_snapshots",
    "process_trunk_snapshots",
]
