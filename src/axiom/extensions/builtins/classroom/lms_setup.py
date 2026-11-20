# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""LMS setup walkthrough — FW-1 P4.

Exposes a compact API the CLI + chat tools call to guide an instructor
through LMS configuration. Wraps the existing
``classroom/lms/`` provider layer; no new provider is created here.

Supported in P4:

- ``canvas`` — fully integrated (CanvasLMSProvider). A ``--fake`` flag
  routes through a populated ``CanvasMockServer`` for testing + demos.
- ``moodle``, ``blackboard`` — listed but marked ``coming-soon``; no
  walkthrough yet. The LMS factory architecture already supports adding
  them as provider classes later.
- ``none`` — explicit opt-out. The classroom is marked as manual-roster.

Entry points:

- ``list_providers()`` — menu data for the CLI / chat tool.
- ``canvas_probe(instance_url, token)`` — connectivity test.
- ``canvas_configure(classroom_id, ...)`` — full configure: probe +
  roster preview + persist on the classroom record.
- ``mark_no_lms(classroom_id)`` — explicit opt-out.
"""

from __future__ import annotations

from typing import Any

SUPPORTED_PROVIDERS: list[str] = ["canvas", "moodle", "blackboard", "none"]


# ---------------------------------------------------------------------------
# list_providers
# ---------------------------------------------------------------------------


_PROVIDER_META: dict[str, dict[str, str]] = {
    "canvas": {
        "display_name": "Canvas",
        "status": "built-in",
        "notes": "Instructure Canvas LMS — roster sync + grade push fully wired.",
    },
    "moodle": {
        "display_name": "Moodle",
        "status": "coming-soon",
        "notes": "Adapter scheduled — contribute a MoodleLMSProvider via extension.",
    },
    "blackboard": {
        "display_name": "Blackboard",
        "status": "coming-soon",
        "notes": "Adapter scheduled — contribute a BlackboardLMSProvider via extension.",
    },
    "none": {
        "display_name": "None (manual roster)",
        "status": "built-in",
        "notes": (
            "Skip LMS integration; drop a students.yaml for the roster. "
            "Suitable for test drives, private cohorts, and Prague-style "
            "external-institution classrooms."
        ),
    },
}


def list_providers() -> list[dict[str, str]]:
    """Return the LMS provider menu."""
    out = []
    for pid in SUPPORTED_PROVIDERS:
        meta = _PROVIDER_META[pid]
        out.append({"id": pid, **meta})
    return out


# ---------------------------------------------------------------------------
# canvas_probe
# ---------------------------------------------------------------------------


def canvas_probe(
    *,
    instance_url: str,
    token: str,
    mock_server: Any | None = None,
) -> dict[str, Any]:
    """Test connectivity to a Canvas instance.

    Returns ``{"connected": bool, "error"?: str}``. Never raises —
    the walkthrough UX shows the error back to the user, not a
    traceback.
    """
    if not instance_url or not token:
        return {
            "connected": False,
            "error": "instance_url and token are both required",
        }

    try:
        from axiom.extensions.builtins.classroom.lms.canvas import (
            CanvasLMSProvider,
        )

        provider = CanvasLMSProvider(
            {
                "name": "canvas-probe",
                "uid": "canvas-probe-ephemeral",
                "api_url": instance_url,
                "api_token": token,
                "_mock_server": mock_server,
            }
        )
        ok = provider.available()
    except Exception as e:
        return {"connected": False, "error": f"probe failed: {e}"}

    if not ok:
        return {
            "connected": False,
            "error": (
                "Canvas instance did not respond. Verify the URL and that "
                "the token is valid + has scope=url:GET|/api/v1/users/self."
            ),
        }
    return {"connected": True, "instance_url": instance_url}


# ---------------------------------------------------------------------------
# canvas_configure
# ---------------------------------------------------------------------------


def canvas_configure(
    *,
    classroom_id: str,
    instance_url: str,
    token: str,
    canvas_course_id: str,
    mock_server: Any | None = None,
) -> dict[str, Any]:
    """Wire a Canvas course to a classroom: probe, pull roster, persist.

    On success, writes roster + lms_provider metadata onto the
    classroom artifact so downstream flows (enrollment, grade push)
    see them. Returns a small summary dict.
    """
    from .operational_store import _reg, load_classroom_data

    data = load_classroom_data(classroom_id)
    if data is None:
        return {"configured": False, "error": f"classroom {classroom_id!r} not found"}

    probe = canvas_probe(
        instance_url=instance_url, token=token, mock_server=mock_server,
    )
    if not probe.get("connected"):
        return {
            "configured": False,
            "error": probe.get("error", "canvas probe failed"),
        }

    try:
        from axiom.extensions.builtins.classroom.lms.canvas import (
            CanvasLMSProvider,
        )

        provider = CanvasLMSProvider(
            {
                "name": "canvas-classroom",
                "uid": f"canvas-classroom-{classroom_id}",
                "api_url": instance_url,
                "api_token": token,
                "_mock_server": mock_server,
            }
        )
        roster = provider.get_roster(canvas_course_id)
    except Exception as e:
        return {"configured": False, "error": f"roster fetch failed: {e}"}

    lms_roster = [
        {
            "id": s.student_id,
            "name": s.name,
            "email": s.email,
            "principal": f"@{s.student_id}:{classroom_id}",
        }
        for s in roster
    ]

    updated = dict(data)
    updated["lms_roster"] = lms_roster
    updated["lms_provider"] = "canvas"
    updated["lms_instance_url"] = instance_url
    updated["lms_course_id"] = canvas_course_id
    _reg().register(kind="classroom", name=classroom_id, data=updated)

    return {
        "configured": True,
        "classroom_id": classroom_id,
        "lms_provider": "canvas",
        "canvas_course_id": canvas_course_id,
        "roster_count": len(lms_roster),
        "roster_preview": lms_roster[:5],
    }


# ---------------------------------------------------------------------------
# mark_no_lms
# ---------------------------------------------------------------------------


def mark_no_lms(*, classroom_id: str) -> dict[str, Any]:
    """Flag the classroom as having no LMS (manual roster path)."""
    from .operational_store import _reg, load_classroom_data

    data = load_classroom_data(classroom_id)
    if data is None:
        return {"no_lms": False, "error": f"classroom {classroom_id!r} not found"}

    updated = dict(data)
    updated["lms_provider"] = "none"
    _reg().register(kind="classroom", name=classroom_id, data=updated)
    return {"no_lms": True, "classroom_id": classroom_id}


# ---------------------------------------------------------------------------
# Test / demo helper — build a populated mock Canvas
# ---------------------------------------------------------------------------


def seed_mock_canvas(
    *,
    courses: list[tuple[str, str]] = (),
    enrollments: dict[str, list[dict]] | None = None,
    offline: bool = False,
) -> Any:
    """Construct a ``CanvasMockServer`` populated for testing + demos.

    Args:
        courses: list of ``(course_id, name)`` tuples to create.
        enrollments: mapping of ``course_id → [enrollment dicts]``.
            Each dict needs ``user_id`` / ``name`` / ``email``; a
            default ``type="student"`` is injected if absent.
        offline: if True, returns a mock whose ``available()`` check
            fails (for "Canvas unreachable" tests).

    The ``--fake`` CLI path uses this helper with a default 5-student
    Classical Mechanics demo course so an instructor running
    ``canvas configure ... --fake`` sees a realistic flow.
    """
    from axiom.extensions.builtins.classroom.lms.canvas_mock import (
        CanvasMockServer,
    )

    mock = CanvasMockServer()

    class _OfflineMock(CanvasMockServer):
        # Subclassing lets canvas_probe's `available()` check see
        # "connected=False" without touching the real Canvas path.
        pass

    if offline:
        # Sentinel: signal offline by setting the url to a value
        # CanvasLMSProvider treats as "no mock" — so it falls through
        # to the real HTTP path, which (with no real server) fails.
        # That's what an offline Canvas actually looks like.
        return None  # caller treats None as "no mock" → network timeout

    for cid, name in courses:
        mock.add_course(cid, name)
    if enrollments:
        for cid, enrolls in enrollments.items():
            for e in enrolls:
                row = dict(e)
                row.setdefault("type", "StudentEnrollment")
                mock.add_enrollment(cid, row)
    return mock


def build_fake_canvas_for_cli() -> Any:
    """Populate a ``CanvasMockServer`` with a canned demo course + 5 students.

    Used by the ``--fake`` CLI path and the ``fake=True`` chat-tool
    arg so instructors evaluating the walkthrough see realistic
    roster output without setting anything up.
    """
    return seed_mock_canvas(
        courses=[("c1", "Classical Mechanics — Spring 26 (fake)")],
        enrollments={
            "c1": [
                {"user_id": "s-demo-1", "name": "Alice Demo", "email": "alice@demo.local"},
                {"user_id": "s-demo-2", "name": "Bob Demo", "email": "bob@demo.local"},
                {"user_id": "s-demo-3", "name": "Carol Demo", "email": "carol@demo.local"},
                {"user_id": "s-demo-4", "name": "Dave Demo", "email": "dave@demo.local"},
                {"user_id": "s-demo-5", "name": "Erin Demo", "email": "erin@demo.local"},
            ],
        },
    )
