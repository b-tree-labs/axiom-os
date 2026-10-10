# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Student readiness gate (WF-2).

Per-student onboarding checklist that must be satisfied before the
student is considered "ready" — meaning: can take assessments,
access EC-gated content, and participate in course-graded activities.

Spec: spec-classroom.md §3.1 WF-2. Integrates with:
- enrollment.py (OnboardingRail, StudentRailChecklist) — reuses the
  rail state model so questionnaire completion advances readiness.
- export control (§5.11.4) — if classroom is EC-gated, nationality
  attestation becomes a hard gate.
- federation (ADR-023) — readiness can be serialized as a signed
  claim for cross-node querying by the instructor's hub node.

Pure data + functions; no I/O. Persistence is the caller's concern.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from .enrollment import OnboardingRail, StudentRailChecklist, load_onboarding_rails

# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass
class StudentReadiness:
    """Per-student onboarding readiness state."""

    student_id: str
    classroom_id: str
    rails: list[StudentRailChecklist] = field(default_factory=list)

    # Hard gates (all required for ready=True)
    syllabus_acknowledged: bool = False
    consent_given: bool = False
    consent_version: str | None = None
    first_chat_completed: bool = False

    # EC gate — only enforced when ec_attestation_required=True
    ec_attestation_required: bool = False
    ec_attestation_signed: bool = False
    ec_nationality: str | None = None
    ec_signed_at: str | None = None

    # Cached rail metadata so we can compute blockers without the manifest
    rail_required: dict[str, bool] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Construction
# ---------------------------------------------------------------------------


def create_student_readiness(
    student_id: str,
    classroom_id: str,
    rails: list[dict] | list[OnboardingRail],
    ec_required: bool = False,
) -> StudentReadiness:
    """Build a readiness object from onboarding rails (dicts or OnboardingRails)."""
    # Normalize: accept raw dicts OR OnboardingRail instances
    if rails and isinstance(rails[0], OnboardingRail):
        rail_objs: list[OnboardingRail] = list(rails)  # type: ignore[arg-type]
    else:
        rail_objs = load_onboarding_rails({"onboarding_rails": list(rails)})

    checklists = [
        StudentRailChecklist(rail_id=r.id, student_id=student_id)
        for r in rail_objs
    ]
    rail_required = {r.id: r.required for r in rail_objs}

    return StudentReadiness(
        student_id=student_id,
        classroom_id=classroom_id,
        rails=checklists,
        ec_attestation_required=ec_required,
        rail_required=rail_required,
    )


# ---------------------------------------------------------------------------
# Markers
# ---------------------------------------------------------------------------


def acknowledge_syllabus(r: StudentReadiness) -> StudentReadiness:
    r = deepcopy(r)
    r.syllabus_acknowledged = True
    return r


def give_consent(r: StudentReadiness, version: str) -> StudentReadiness:
    r = deepcopy(r)
    r.consent_given = True
    r.consent_version = version
    return r


def complete_first_chat(r: StudentReadiness) -> StudentReadiness:
    r = deepcopy(r)
    r.first_chat_completed = True
    return r


def sign_ec_attestation(
    r: StudentReadiness, nationality: str, signed_by: str
) -> StudentReadiness:
    r = deepcopy(r)
    r.ec_attestation_signed = True
    r.ec_nationality = nationality
    r.ec_signed_at = datetime.now(UTC).isoformat()
    return r


def record_rail_response(
    r: StudentReadiness, rail_id: str, question_id: str, answer: Any
) -> StudentReadiness:
    r = deepcopy(r)
    for rail in r.rails:
        if rail.rail_id == rail_id:
            rail.responses[question_id] = answer
            if rail.status == "pending":
                rail.status = "in_progress"
            return r
    raise ValueError(f"rail {rail_id!r} not found")


def complete_rail(r: StudentReadiness, rail_id: str) -> StudentReadiness:
    r = deepcopy(r)
    for rail in r.rails:
        if rail.rail_id == rail_id:
            rail.status = "completed"
            return r
    raise ValueError(f"rail {rail_id!r} not found")


def skip_rail(r: StudentReadiness, rail_id: str) -> StudentReadiness:
    r = deepcopy(r)
    if r.rail_required.get(rail_id, False):
        raise ValueError(f"rail {rail_id!r} is required — cannot skip")
    for rail in r.rails:
        if rail.rail_id == rail_id:
            rail.status = "skipped"
            return r
    raise ValueError(f"rail {rail_id!r} not found")


# ---------------------------------------------------------------------------
# Readiness gate
# ---------------------------------------------------------------------------


def is_student_ready(r: StudentReadiness) -> tuple[bool, list[str]]:
    """Return (ready, blockers). Student is ready when all hard gates pass."""
    blockers: list[str] = []

    if not r.syllabus_acknowledged:
        blockers.append("syllabus_acknowledged: student has not acknowledged syllabus")

    if not r.consent_given:
        blockers.append("consent_given: student has not given data-use consent")

    if not r.first_chat_completed:
        blockers.append("first_chat_completed: student has not completed first chat exercise")

    if r.ec_attestation_required and not r.ec_attestation_signed:
        blockers.append(
            "ec_attestation_signed: student has not signed nationality attestation "
            "(required for EC-gated course)"
        )

    for rail in r.rails:
        if r.rail_required.get(rail.rail_id, False):
            if rail.status not in ("completed", "skipped"):
                blockers.append(
                    f"rail_{rail.rail_id}: required onboarding rail "
                    f"{rail.rail_id!r} is {rail.status}"
                )

    return len(blockers) == 0, blockers


# ---------------------------------------------------------------------------
# Cohort aggregate
# ---------------------------------------------------------------------------


def cohort_readiness_report(readinesses: list[StudentReadiness]) -> dict:
    """Summarize readiness across a cohort (instructor-facing)."""
    rows = []
    ready_count = 0
    for r in readinesses:
        ready, blockers = is_student_ready(r)
        if ready:
            ready_count += 1
        rows.append({
            "student_id": r.student_id,
            "ready": ready,
            "blockers": blockers,
            "syllabus": r.syllabus_acknowledged,
            "consent": r.consent_given,
            "first_chat": r.first_chat_completed,
            "ec_signed": r.ec_attestation_signed if r.ec_attestation_required else None,
            "rails": {rc.rail_id: rc.status for rc in r.rails},
        })
    return {
        "total": len(readinesses),
        "ready": ready_count,
        "not_ready": len(readinesses) - ready_count,
        "students": rows,
    }


# ---------------------------------------------------------------------------
# Federation serialization (ADR-023)
# ---------------------------------------------------------------------------


def serialize_readiness_claim(
    r: StudentReadiness, signer_node: str
) -> dict:
    """Produce a signed-claim dict for cross-node federation transport.

    The signature slot is reserved for the federation layer (which
    holds the node's signing key); this module only assembles the
    payload. Receivers verify the signer_node against the trust
    chain before accepting the claim.
    """
    ready, blockers = is_student_ready(r)
    return {
        "student_id": r.student_id,
        "classroom_id": r.classroom_id,
        "signer_node": signer_node,
        "issued_at": datetime.now(UTC).isoformat(),
        "ready": ready,
        "blockers": blockers,
        "syllabus_acknowledged": r.syllabus_acknowledged,
        "consent_given": r.consent_given,
        "consent_version": r.consent_version,
        "first_chat_completed": r.first_chat_completed,
        "ec_attestation_required": r.ec_attestation_required,
        "ec_attestation_signed": r.ec_attestation_signed,
        "ec_nationality": r.ec_nationality,
        "rails": {rc.rail_id: rc.status for rc in r.rails},
        "signature": None,  # federation layer fills this in
    }
