# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""``state.snapshot_tick`` -- the scheduled snapshot (PULSE cadence).

Declared in the manifest as an interval cadence; the interval that actually
governs is ``state.snapshots.interval_hours`` (default 6), checked here, so
changing it is a settings edit and not a re-registration.  "While the harness
is active" falls out of the unchanged-inputs skip: an idle harness produces no
new snapshots.
"""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any

from axiom.infra import backup_core as bc
from axiom.infra import state_snapshots as ss
from axiom.infra.skills import SkillContext, SkillResult

from ._common import resolve

log = logging.getLogger(__name__)


def run(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    state_dir, root, policy, store = resolve(params, ctx)
    if not policy.enabled:
        return SkillResult(ok=True, actions_taken=["state snapshots disabled (state.snapshots.enabled=false)"])

    latest_id = next(iter(ss.newest_first(store)), None)  # most recently TAKEN, not most recently dated
    newest = next((r for r in ss.list_snapshots(store) if r["id"] == latest_id), None)
    if newest and newest.get("created_at") and not params.get("force"):
        age = bc.utcnow() - bc.parse_iso(newest["created_at"])
        # A snapshot dated in the future (the clock was wrong) says nothing about how long ago it was
        # taken: treat it as no trustworthy snapshot, so one is due now rather than skipped for days.
        if -bc._FUTURE_TOLERANCE <= age < timedelta(hours=policy.interval_hours) * 0.9:
            return SkillResult(ok=True, actions_taken=[
                f"newest snapshot {newest['id']} is {age} old; interval is {policy.interval_hours}h"])
    try:
        r = ss.create_snapshot(state_dir, root, reason="scheduled", trigger="schedule", policy=policy)
    except ss.SnapshotError as exc:
        # Loud on purpose: a backup job that fails quietly is the failure mode.
        log.error("SCHEDULED STATE SNAPSHOT FAILED: %s", exc)
        return SkillResult(ok=False, errors=[f"scheduled state snapshot failed: {exc}"])
    v = ss.verify_store(store, deep=False)
    errs = [] if v.ok else ["snapshot store failed verification: run `state verify`"]
    if errs:
        log.error("%s", errs[0])
    return SkillResult(ok=v.ok, errors=errs, value=ss.snapshot_dict(r), actions_taken=[r.message])
