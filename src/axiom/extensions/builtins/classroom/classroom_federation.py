# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Classroom ↔ federation primitive mapping (ADR-022/023, spec §5.11).

A Classroom is an ephemeral federation cohort:
- Coordinator node = instructor's node (where `axi classroom create` ran)
- Member nodes = student nodes (one per student)

Pure data + functional transitions. The actual A2A transport
(how `broadcast_recipients` turns into network messages) lives in
the federation layer; this module only models cohort state +
serializes proofs for cross-node consumption.

States (aligning with ADR-025 quarantine/recovery lattice):
    ACTIVE → QUARANTINED → (recovered) → ACTIVE
    ACTIVE → REVOKED  (terminal)

Quarantine cascade: when the federation layer quarantines a node
(trust-chain break, attestation expiry, behavior anomaly), the
classroom layer reads that signal and suspends the member's
classroom access. Recovery requires an approver-signed step.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from datetime import UTC, datetime

# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass
class CohortMember:
    student_id: str
    member_node: str
    invite_token: str
    status: str = "ACTIVE"  # ACTIVE, QUARANTINED, REVOKED
    joined_at: str | None = None

    quarantine_reason: str | None = None
    quarantined_at: str | None = None

    recovery_approver: str | None = None
    recovered_at: str | None = None

    revoked_reason: str | None = None
    revoked_at: str | None = None


@dataclass
class ClassroomCohort:
    """Ephemeral federation cohort for a single classroom."""

    classroom_id: str
    coordinator_node: str
    members: list[CohortMember] = field(default_factory=list)
    created_at: str | None = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _find_member(cohort: ClassroomCohort, student_id: str) -> CohortMember:
    for m in cohort.members:
        if m.student_id == student_id:
            return m
    raise ValueError(f"student {student_id!r} not in cohort {cohort.classroom_id!r}")


# ---------------------------------------------------------------------------
# Cohort lifecycle
# ---------------------------------------------------------------------------


def create_cohort(classroom_id: str, coordinator_node: str) -> ClassroomCohort:
    """Spin up an empty cohort with the coordinator node set."""
    return ClassroomCohort(
        classroom_id=classroom_id,
        coordinator_node=coordinator_node,
        created_at=_now_iso(),
    )


def add_member(
    cohort: ClassroomCohort,
    student_id: str,
    member_node: str,
    invite_token: str,
) -> ClassroomCohort:
    """Add (or update) a cohort member."""
    c = deepcopy(cohort)
    for m in c.members:
        if m.student_id == student_id:
            m.member_node = member_node
            m.invite_token = invite_token
            return c
    c.members.append(CohortMember(
        student_id=student_id,
        member_node=member_node,
        invite_token=invite_token,
        joined_at=_now_iso(),
    ))
    return c


# ---------------------------------------------------------------------------
# Trust state transitions
# ---------------------------------------------------------------------------


def quarantine_member(
    cohort: ClassroomCohort,
    student_id: str,
    reason: str,
) -> ClassroomCohort:
    c = deepcopy(cohort)
    m = _find_member(c, student_id)
    if m.status == "REVOKED":
        raise ValueError(
            f"cannot quarantine a REVOKED member ({student_id})"
        )
    m.status = "QUARANTINED"
    m.quarantine_reason = reason
    m.quarantined_at = _now_iso()
    return c


def recover_member(
    cohort: ClassroomCohort,
    student_id: str,
    approver: str,
) -> ClassroomCohort:
    c = deepcopy(cohort)
    m = _find_member(c, student_id)
    if m.status == "REVOKED":
        raise ValueError(
            f"cannot recover a REVOKED member ({student_id}); revocation is terminal"
        )
    if m.status != "QUARANTINED":
        raise ValueError(
            f"can only recover QUARANTINED members; {student_id} is {m.status}"
        )
    m.status = "ACTIVE"
    m.recovery_approver = approver
    m.recovered_at = _now_iso()
    return c


def revoke_member(
    cohort: ClassroomCohort,
    student_id: str,
    reason: str,
) -> ClassroomCohort:
    c = deepcopy(cohort)
    m = _find_member(c, student_id)
    m.status = "REVOKED"
    m.revoked_reason = reason
    m.revoked_at = _now_iso()
    return c


# ---------------------------------------------------------------------------
# Access check + broadcast
# ---------------------------------------------------------------------------


def member_has_access(cohort: ClassroomCohort, student_id: str) -> bool:
    """True iff member is ACTIVE."""
    try:
        m = _find_member(cohort, student_id)
    except ValueError:
        return False
    return m.status == "ACTIVE"


def broadcast_recipients(cohort: ClassroomCohort) -> list[str]:
    """Return the list of member_node addresses eligible for broadcasts."""
    return [m.member_node for m in cohort.members if m.status == "ACTIVE"]


# ---------------------------------------------------------------------------
# Membership proof (for cross-node handshake)
# ---------------------------------------------------------------------------


def serialize_membership_proof(
    cohort: ClassroomCohort,
    student_id: str,
) -> dict:
    """Produce a signed-claim payload asserting member's cohort membership.

    Receiver nodes verify the claim's signature against the cohort's
    coordinator trust chain before granting classroom-scoped access.
    Signature slot reserved for federation layer.
    """
    m = _find_member(cohort, student_id)
    return {
        "student_id": student_id,
        "classroom_id": cohort.classroom_id,
        "coordinator_node": cohort.coordinator_node,
        "member_node": m.member_node,
        "status": m.status,
        "issued_at": _now_iso(),
        "signature": None,  # federation layer fills in
    }
