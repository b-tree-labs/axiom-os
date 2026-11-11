# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Course lifecycle state machine + semver enforcement.

Per spec-classroom.md §2.6: Courses transition through
draft → review → published → deprecated. Version bumps follow
semver — major/minor/patch with well-defined semantics.

Pure functional API: transitions return a new CourseState. Callers
persist via course state files or integrate with ArtifactRegistry
via the optional `registry` callback (ADR-ish — decoupled so the
registry can be swapped or absent).
"""

from __future__ import annotations

import re
from collections.abc import Callable
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import UTC, datetime

# ---------------------------------------------------------------------------
# Semver
# ---------------------------------------------------------------------------


_SEMVER_RE = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")


def parse_semver(version: str) -> tuple[int, int, int]:
    """Parse `major.minor.patch` → (major, minor, patch)."""
    m = _SEMVER_RE.match(version)
    if not m:
        raise ValueError(f"invalid semver: {version!r} (expected 'major.minor.patch')")
    return (int(m.group(1)), int(m.group(2)), int(m.group(3)))


def bump_version(version: str, bump_type: str) -> str:
    """Bump semver. bump_type is 'major', 'minor', or 'patch'."""
    major, minor, patch = parse_semver(version)
    if bump_type == "major":
        return f"{major + 1}.0.0"
    if bump_type == "minor":
        return f"{major}.{minor + 1}.0"
    if bump_type == "patch":
        return f"{major}.{minor}.{patch + 1}"
    raise ValueError(f"invalid bump type: {bump_type!r} (expected major|minor|patch)")


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------


VALID_STATUSES = ("draft", "review", "published", "deprecated")


@dataclass
class CourseState:
    """Course lifecycle state + version history."""

    course_id: str
    version: str  # current version
    status: str = "draft"

    submitted_by: str | None = None
    submitted_at: str | None = None
    published_by: str | None = None
    published_at: str | None = None
    deprecated_at: str | None = None
    deprecation_reason: str | None = None

    version_history: list[dict] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Transitions
# ---------------------------------------------------------------------------


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def submit_for_review(state: CourseState, submitter: str) -> CourseState:
    """draft → review."""
    if state.status != "draft":
        raise ValueError(
            f"can only submit a draft for review; current status is {state.status!r}"
        )
    s = deepcopy(state)
    s.status = "review"
    s.submitted_by = submitter
    s.submitted_at = _now_iso()
    return s


def publish(
    state: CourseState,
    approver: str,
    registry: Callable[[dict], None] | None = None,
) -> CourseState:
    """review → published. Bumps to 1.0.0 if publishing from pre-1.0."""
    if state.status != "review":
        raise ValueError(
            f"can only publish a course in review; current status is {state.status!r}"
        )
    s = deepcopy(state)
    major, minor, patch = parse_semver(s.version)
    if major == 0:
        s.version = "1.0.0"
    s.status = "published"
    s.published_by = approver
    s.published_at = _now_iso()
    s.version_history.append({
        "version": s.version,
        "published_at": s.published_at,
        "published_by": approver,
        "notes": "initial publish",
    })
    if registry:
        registry({
            "type": "course_published",
            "course_id": s.course_id,
            "version": s.version,
            "published_at": s.published_at,
        })
    return s


def republish_with_bump(
    state: CourseState,
    bump_type: str,
    approver: str,
    notes: str,
    registry: Callable[[dict], None] | None = None,
) -> CourseState:
    """Bump version + re-publish. Only valid from a published state."""
    if state.status != "published":
        raise ValueError(
            f"can only republish a published course; current status is {state.status!r}"
        )
    s = deepcopy(state)
    s.version = bump_version(s.version, bump_type)
    s.published_at = _now_iso()
    s.published_by = approver
    s.version_history.append({
        "version": s.version,
        "published_at": s.published_at,
        "published_by": approver,
        "notes": notes,
    })
    if registry:
        registry({
            "type": "course_published",
            "course_id": s.course_id,
            "version": s.version,
            "published_at": s.published_at,
        })
    return s


def deprecate(state: CourseState, reason: str) -> CourseState:
    """published → deprecated."""
    if state.status != "published":
        raise ValueError(
            f"can only deprecate a published course; current status is {state.status!r}"
        )
    s = deepcopy(state)
    s.status = "deprecated"
    s.deprecated_at = _now_iso()
    s.deprecation_reason = reason
    return s
