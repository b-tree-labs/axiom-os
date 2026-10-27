# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Budget state, its transitions, and what landing may do in each state.

``ok`` lands as usual. ``warn`` lands in batches, skips optional re-runs and
refuses blind retries. ``stop`` pauses every CI-triggering action that is not
a release, and alerts. ``unknown`` (billing unreadable) behaves like ``ok``
and says so: RIVET does not invent a number.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from axiom.extensions.builtins.release.ci_health.config import CIHealthConfig

OK, WARN, STOP, UNKNOWN = "ok", "warn", "stop", "unknown"
_RANK = {UNKNOWN: 0, OK: 0, WARN: 1, STOP: 2}


def budget_state(pct: float | None, config: CIHealthConfig) -> str:
    if pct is None:
        return UNKNOWN
    if pct >= config.stop_pct:
        return STOP
    if pct >= config.warn_pct:
        return WARN
    return OK


@dataclass(frozen=True)
class Transition:
    previous: str
    current: str

    @property
    def escalated(self) -> bool:
        return _RANK[self.current] > _RANK[self.previous]

    @property
    def relaxed(self) -> bool:
        return _RANK[self.current] < _RANK[self.previous]


def transition(previous: str | None, current: str) -> Transition:
    return Transition(previous or UNKNOWN, current)


@dataclass(frozen=True)
class LandingPolicy:
    state: str
    allow_rerun: bool
    allow_repair: bool
    allow_update_branch: bool
    release_only: bool
    batch_min: int
    batch_max_wait_hours: float
    reason: str
    release_labels: tuple[str, ...] = ()
    release_branch_prefixes: tuple[str, ...] = ()

    def to_dict(self) -> dict:
        return asdict(self)


def landing_policy(state: str, config: CIHealthConfig) -> LandingPolicy:
    release = {"release_labels": config.release_labels, "release_branch_prefixes": config.release_branch_prefixes}
    if state == STOP:
        return LandingPolicy(
            STOP, False, False, False, True, 1, 0.0,
            "Actions budget is at the stop line: only release pull requests land",
            **release,
        )
    if state == WARN:
        return LandingPolicy(
            WARN, False, True, True, False, config.warn_batch_min, config.batch_max_wait_hours,
            "Actions budget is past the warn line: land in batches, no blind retries",
            **release,
        )
    return LandingPolicy(state, True, True, True, False, 1, 0.0, "", **release)


def is_release_pull(pull, config) -> bool:
    """``config`` is a ``CIHealthConfig`` or a ``LandingPolicy``; both name the release markers."""
    labels = {label.lower() for label in getattr(pull, "labels", ())}
    if labels & {label.lower() for label in config.release_labels}:
        return True
    ref = str(getattr(pull, "head_ref", "") or "")
    return any(ref.startswith(prefix) for prefix in config.release_branch_prefixes)


def batch_gate(pulls, policy: LandingPolicy, *, now: datetime | None = None) -> tuple[bool, str]:
    """Whether this landing pass should run now. At warn, wait for a batch."""
    if policy.batch_min <= 1:
        return True, ""
    now = now or datetime.now(UTC)
    ready = [p for p in pulls if getattr(p, "checks", "") == "passing" and not getattr(p, "draft", False)]
    if len(ready) >= policy.batch_min:
        return True, f"batch of {len(ready)} ready"
    if ready:
        oldest = min(p.updated_at for p in ready)
        if now - oldest >= timedelta(hours=policy.batch_max_wait_hours):
            return True, f"{len(ready)} ready, oldest waited {policy.batch_max_wait_hours:g}h"
    return False, f"batching: {len(ready)} of {policy.batch_min} ready"


def _state_path(state_dir: Path) -> Path:
    return Path(state_dir) / "agents" / "rivet" / "ci-health.json"


def save_state(state_dir: Path, payload: dict) -> None:
    path = _state_path(state_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, indent=2, default=str) + "\n", encoding="utf-8")
    tmp.replace(path)


def load_state(state_dir: Path) -> dict:
    try:
        data = json.loads(_state_path(state_dir).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def current_state(state_dir: Path, config: CIHealthConfig, *, environ=None, now: datetime | None = None) -> str:
    """The budget state ``land --act`` obeys.

    ``AXIOM_CI_BUDGET_STATE`` wins (a runner without billing scope can be told).
    Otherwise the last sweep's state, if it is fresher than ``state_ttl_hours``.
    """
    env = os.environ if environ is None else environ
    forced = str(env.get("AXIOM_CI_BUDGET_STATE") or "").strip().lower()
    if forced in {OK, WARN, STOP}:
        return forced
    data = load_state(state_dir)
    stamp = data.get("checked_at")
    state = data.get("budget_state")
    if not isinstance(stamp, str) or state not in {OK, WARN, STOP}:
        return UNKNOWN
    try:
        when = datetime.fromisoformat(stamp)
    except ValueError:
        return UNKNOWN
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    if (now or datetime.now(UTC)) - when > timedelta(hours=config.state_ttl_hours):
        return UNKNOWN
    return state
