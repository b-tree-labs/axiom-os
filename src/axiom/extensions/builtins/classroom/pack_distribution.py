# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Federated course pack distribution (§5.9).

Tracks per-member pinned course pack version + pending updates.
When an instructor publishes a new version of the course, this
module computes the per-member update plan and produces the
broadcast payload for the federation layer to transport.

Opt-in updates: students explicitly `accept_update` to move to a
new version — prevents surprise mid-session changes that could
break ongoing student work. Quarantined members are excluded from
broadcasts (enforced via classroom_federation.broadcast_recipients).

Traces annotated with pack_version so session replays reconstruct
the exact state a student saw — even after mid-course updates.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from datetime import UTC, datetime

from .classroom_federation import (
    ClassroomCohort,
    broadcast_recipients,
)
from .course_lifecycle import parse_semver

# ---------------------------------------------------------------------------
# Distribution state
# ---------------------------------------------------------------------------


@dataclass
class PackDistribution:
    """Per-classroom pack distribution state."""

    classroom_id: str
    active_version: str              # instructor-published latest
    pinned_versions: dict[str, str] = field(default_factory=dict)
    # student_id → version they last accepted
    pending_updates: dict[str, dict] = field(default_factory=dict)
    # student_id → {"from": old, "to": new, "notes": str, "published_at": iso}


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def init_distribution(
    cohort: ClassroomCohort,
    initial_version: str,
) -> PackDistribution:
    """Pin every active member to the initial version."""
    pinned = {
        m.student_id: initial_version
        for m in cohort.members
        if m.status == "ACTIVE"
    }
    return PackDistribution(
        classroom_id=cohort.classroom_id,
        active_version=initial_version,
        pinned_versions=pinned,
    )


# ---------------------------------------------------------------------------
# Publish update
# ---------------------------------------------------------------------------


def publish_update(
    dist: PackDistribution,
    cohort: ClassroomCohort,
    new_version: str,
    notes: str,
) -> PackDistribution:
    """Record a new version + create pending updates for active members.

    Refuses a version downgrade (must be higher semver than current
    active). Quarantined/revoked members are excluded from pending
    updates; they'll be re-evaluated when recovered.
    """
    current = parse_semver(dist.active_version)
    new = parse_semver(new_version)
    if new <= current:
        raise ValueError(
            f"cannot downgrade or re-publish same version "
            f"({dist.active_version} → {new_version})"
        )

    d = deepcopy(dist)
    d.active_version = new_version
    published_at = _now_iso()

    for m in cohort.members:
        if m.status != "ACTIVE":
            continue
        if d.pinned_versions.get(m.student_id) == new_version:
            continue
        d.pending_updates[m.student_id] = {
            "from": d.pinned_versions.get(m.student_id, "unknown"),
            "to": new_version,
            "notes": notes,
            "published_at": published_at,
        }
    return d


# ---------------------------------------------------------------------------
# Accept update
# ---------------------------------------------------------------------------


def accept_update(
    dist: PackDistribution,
    student_id: str,
) -> PackDistribution:
    """Student accepts the pending update → pinned version advances."""
    d = deepcopy(dist)
    pending = d.pending_updates.pop(student_id, None)
    if pending is None:
        return d  # no-op if no pending
    d.pinned_versions[student_id] = pending["to"]
    return d


# ---------------------------------------------------------------------------
# Lookup helpers
# ---------------------------------------------------------------------------


def pack_version_for_student(
    dist: PackDistribution,
    student_id: str,
) -> str | None:
    return dist.pinned_versions.get(student_id)


# ---------------------------------------------------------------------------
# Trace annotation
# ---------------------------------------------------------------------------


def annotate_trace_with_pack_version(
    trace: dict,
    dist: PackDistribution,
) -> dict:
    """Add `pack_version` to a trace based on the student's pin."""
    out = dict(trace)
    sid = trace.get("student_id")
    if sid:
        v = pack_version_for_student(dist, sid)
        if v:
            out["pack_version"] = v
    return out


def annotate_traces_with_pack_version(
    traces: list[dict],
    dist: PackDistribution,
) -> list[dict]:
    return [annotate_trace_with_pack_version(t, dist) for t in traces]


# ---------------------------------------------------------------------------
# Broadcast payload
# ---------------------------------------------------------------------------


def build_update_broadcast(
    dist: PackDistribution,
    cohort: ClassroomCohort,
    pack_path: str,
) -> dict:
    """Build the federation broadcast payload for a pack update.

    The actual transport (how `recipients` addresses become network
    messages) lives in the federation layer. This function just
    assembles the payload + reserves the signature slot.
    """
    return {
        "classroom_id": cohort.classroom_id,
        "coordinator_node": cohort.coordinator_node,
        "new_version": dist.active_version,
        "pack_path": pack_path,
        "recipients": broadcast_recipients(cohort),
        "issued_at": _now_iso(),
        "signature": None,
    }
