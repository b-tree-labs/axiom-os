# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Enrollment runner — Keplo P6a.

Wraps ``enrollment.enroll_classroom`` with the lookups + validation
the CLI/chat tool need: load classroom record, require PUBLISHED
state, resolve the LMS provider (real or fake), shape the result for
JSON output.

The core enrollment logic stays in ``enrollment.py``; this module is
just the CLI/tool adapter.
"""

from __future__ import annotations

from dataclasses import asdict
from typing import Any


def run_enrollment(
    *,
    classroom_id: str,
    instructor: str,
    fake: bool = False,
    ttl_days: int = 30,
    canvas_course_id: str | None = None,
) -> dict[str, Any]:
    """Run WF-1 enrollment against a published classroom.

    Guards:
    - Classroom must exist and be PUBLISHED (unpublished = still in
      prep; enrolling would produce orphan tokens).
    - ``fake=True`` uses a populated CanvasMockServer — the same one
      the P4 LMS walkthrough uses — so Prague-style demos work
      without a real Canvas.
    - ``canvas_course_id`` defaults to the classroom's stored
      ``lms_course_id`` from the P4 configure step.

    Returns a dict suitable for JSON serialization. ``enrolled`` is
    the outcome flag; ``error`` carries the reason on failure.
    """
    from .lms_setup import build_fake_canvas_for_cli
    from .operational_store import load_classroom_data, load_course_data
    from .publish import PUBLISHED

    if not classroom_id or not instructor:
        return {
            "enrolled": False,
            "error": "classroom_id and instructor are both required",
        }

    classroom = load_classroom_data(classroom_id)
    if classroom is None:
        return {
            "enrolled": False,
            "error": f"classroom {classroom_id!r} not found",
        }

    state = classroom.get("state") or "unpublished"
    if state != PUBLISHED:
        return {
            "enrolled": False,
            "error": (
                f"classroom must be published before enrollment "
                f"(current state: {state!r}). Run `axi classroom "
                f"publish {classroom_id}` first."
            ),
        }

    # Resolve the Canvas course id: explicit > classroom.lms_course_id
    # > "mock-course-1" for the --fake path.
    course_id_for_canvas = (
        canvas_course_id
        or classroom.get("lms_course_id")
        or ("c1" if fake else None)
    )
    if not course_id_for_canvas:
        return {
            "enrolled": False,
            "error": (
                "no canvas_course_id available; run "
                "`axi classroom prep lms-setup canvas-configure` to "
                "bind a Canvas course, or supply --canvas-course-id "
                "explicitly"
            ),
        }

    # Build the LMS provider — fake or real.
    if fake:
        from .lms.canvas import CanvasLMSProvider

        mock = build_fake_canvas_for_cli()
        lms_provider = CanvasLMSProvider(
            {
                "name": "canvas-enroll-fake",
                # Deterministic uid silences the "no uid in config" warning
                # that fires on every fake-mode invocation otherwise.
                "uid": "canvas-enroll-fake-ephemeral",
                "api_url": "mock://canvas",
                "api_token": "fake",
                "_mock_server": mock,
            }
        )
    else:
        from axiom.integrations.lms.env import build_lms_provider_from_env

        lms_provider = build_lms_provider_from_env()
        if lms_provider is None:
            return {
                "enrolled": False,
                "error": (
                    "no LMS provider configured; set AXIOM_CANVAS_API_URL"
                    " + AXIOM_CANVAS_API_TOKEN or rerun with --fake"
                ),
            }

    # Pull the course manifest so onboarding rails flow through.
    course_data = (
        load_course_data(classroom.get("course_id", "")) or {}
    )
    course_manifest = course_data.get("manifest") or {}

    from .enrollment import enroll_classroom

    try:
        result = enroll_classroom(
            lms_provider=lms_provider,
            canvas_course_id=course_id_for_canvas,
            classroom_id=classroom_id,
            ttl_days=ttl_days,
            instructor_email=instructor,
            course_manifest=course_manifest,
        )
    except Exception as e:
        return {"enrolled": False, "error": f"enrollment failed: {e}"}

    return {
        "enrolled": True,
        "classroom_id": classroom_id,
        "instructor": instructor,
        "ttl_days": ttl_days,
        "student_count": len(result.students),
        "tokens": [
            {
                "student_id": t.student_id,
                "name": t.name,
                "email": t.email,
                "token": t.token,
                "classroom_id": t.classroom_id,
                "ttl_days": t.ttl_days,
                "issued_at": t.issued_at,
                "expires_at": t.expires_at,
            }
            for t in result.tokens
        ],
        "attestations": [asdict(a) for a in result.attestations],
        "rail_count": len(result.checklists[0]) if result.checklists else 0,
    }
