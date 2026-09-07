# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Course-conclusion utilities — FW-4.

Reads from the classroom's operational store + grade ledger to produce
cohort summaries, research-harvest bundles, and template-update
proposals. Pure reads in P2 (analytics); writes arrive in later
phases.

The intent is that every FW-4 phase composes on the same runtime
primitives — no new state machine. Analytics is inspection over
what's already stored; harvest is export; template-update is a
proposed diff.
"""

from __future__ import annotations

import json
import os
import statistics
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Grade ledger reader — matches grade_push's file layout
# ---------------------------------------------------------------------------


def _runtime_root() -> Path:
    override = os.environ.get("AXIOM_RUNTIME_ROOT")
    if override:
        return Path(override)
    try:
        from axiom import REPO_ROOT  # type: ignore

        return Path(REPO_ROOT) / "runtime"
    except Exception:
        return Path.cwd() / "runtime"


def _grade_ledger_dir(classroom_id: str) -> Path:
    return _runtime_root() / "classrooms" / classroom_id / "grades"


def _read_grade_ledger(classroom_id: str) -> list[dict[str, Any]]:
    """Return per-assessment grade-ledger dicts, one per file."""
    gdir = _grade_ledger_dir(classroom_id)
    if not gdir.is_dir():
        return []
    out: list[dict[str, Any]] = []
    for entry in sorted(gdir.iterdir()):
        if not entry.is_file() or entry.suffix != ".json":
            continue
        try:
            out.append(json.loads(entry.read_text()))
        except (OSError, json.JSONDecodeError):
            continue
    return out


def _aggregate_grade_ledger(
    ledger: list[dict[str, Any]],
) -> dict[str, Any]:
    """Compute per-assessment score distributions."""
    assessments: list[dict[str, Any]] = []
    total = 0
    for entry in ledger:
        grades = [
            g.get("score")
            for g in entry.get("grades", [])
            if isinstance(g.get("score"), (int, float))
        ]
        if not grades:
            continue
        total += len(grades)
        mean = statistics.mean(grades)
        stdev = statistics.pstdev(grades) if len(grades) > 1 else 0.0
        assessments.append(
            {
                "assessment_id": entry.get("assessment_id", "(unknown)"),
                "count": len(grades),
                "mean": round(mean, 4),
                "stdev": round(stdev, 4),
                "min": round(min(grades), 4),
                "max": round(max(grades), 4),
                "median": round(statistics.median(grades), 4),
            }
        )
    return {"assessments": assessments, "total_graded": total}


# ---------------------------------------------------------------------------
# summarize_classroom — the P2 entry point
# ---------------------------------------------------------------------------


def summarize_classroom(classroom_id: str) -> dict[str, Any]:
    """Produce a cohort summary for the instructor's post-course review.

    Read-only. Works at any classroom state (published or archived).
    Aggregates:

    - Classroom identity + current lifecycle state + timestamps.
    - Roster composition (size + principal ids).
    - Course-config snapshot (checkpoints, assessments, rails, system-
      prompt presence).
    - Grade-ledger summary (per-assessment distributions) if ledger
      files exist under runtime/classrooms/<id>/grades/.

    Returns ``{"error": ...}`` on unknown classroom.
    """
    from .operational_store import load_classroom_data, load_course_data

    classroom = load_classroom_data(classroom_id)
    if classroom is None:
        return {"error": f"classroom {classroom_id!r} not found"}

    course_id = classroom.get("course_id", "")
    course = load_course_data(course_id) if course_id else None

    roster = classroom.get("lms_roster") or []
    roster_ids = [s.get("id", "") for s in roster]

    manifest = (course or {}).get("manifest") or {}
    checkpoints = list(manifest.get("checkpoints") or [])
    rails = list(
        (course or {}).get("rails")
        or manifest.get("rails")
        or []
    )
    assessments = list((course or {}).get("assessments") or [])

    ledger = _read_grade_ledger(classroom_id)
    grade_ledger = _aggregate_grade_ledger(ledger)

    summary: dict[str, Any] = {
        "classroom_id": classroom_id,
        "course_id": course_id,
        "title": classroom.get("title", ""),
        "state": classroom.get("state") or "unpublished",
        "published_at": classroom.get("published_at"),
        "archived_at": classroom.get("archived_at"),
        "roster": {
            "size": len(roster),
            "student_ids": roster_ids,
        },
        "course_config": {
            "checkpoints": len(checkpoints),
            "assessments": len(assessments),
            "rails": len(rails),
            "has_system_prompt": bool(
                (course or {}).get("system_prompt")
            ),
        },
        "grade_ledger": grade_ledger,
    }
    return summary


def export_classroom(
    *, classroom_id: str, out_path: Path | str,
) -> dict[str, Any]:
    """Bundle a classroom's verbatim coordinator state into a `.tar.gz`.

    Distinct from :func:`harvest_classroom` — this is the *instructor's*
    keepsake export. No anonymization, full fidelity:

      - ``classroom.json`` — operational record (state, lifecycle,
        approvers, archiver, etc.)
      - ``course.json`` — course record
      - ``coordinator/`` — the on-disk classroom state directory
        (materials/, briefs.jsonl, threads.jsonl, quizzes.jsonl,
        interactions.jsonl, ...)
      - ``MANIFEST.json`` — bundle metadata (classroom_id, exported_at,
        contents listing)

    Use this when the semester is over and the instructor wants a
    self-contained artifact to archive elsewhere (Box, Google Drive,
    INL repository). Pair with ``axi classroom archive`` for a clean
    end-of-semester ceremony: archive flips state to terminal, export
    captures the snapshot.

    Returns ``{"exported": bool, "path"?: str, "error"?: str}``.
    """
    import tarfile
    from datetime import UTC, datetime

    from .operational_store import load_classroom_data, load_course_data

    out = Path(out_path)

    classroom = load_classroom_data(classroom_id)
    if classroom is None:
        return {
            "exported": False,
            "error": f"classroom {classroom_id!r} not found",
        }
    course_id = classroom.get("course_id", "")
    course = load_course_data(course_id) if course_id else {}

    coord_classroom_dir = (
        Path.home() / ".axi" / "coordinator"
        / "classrooms" / classroom_id
    )
    cohort_path = (
        Path.home() / ".axi" / "coordinator" / "cohorts" / f"{classroom_id}.json"
    )

    exported_at = datetime.now(UTC).isoformat()
    manifest: dict[str, Any] = {
        "bundle": "classroom-export",
        "classroom_id": classroom_id,
        "course_id": course_id,
        "title": classroom.get("title", ""),
        "state": classroom.get("state", "unpublished"),
        "exported_at": exported_at,
        "format_version": 1,
        "contents": ["MANIFEST.json", "classroom.json", "course.json"],
    }

    if coord_classroom_dir.is_dir():
        manifest["contents"].append("coordinator/")
    if cohort_path.is_file():
        manifest["contents"].append("cohort.json")

    try:
        out.parent.mkdir(parents=True, exist_ok=True)
        with tarfile.open(out, "w:gz") as tar:
            def _add_bytes(name: str, payload: bytes) -> None:
                info = tarfile.TarInfo(name=name)
                info.size = len(payload)
                info.mtime = int(datetime.now(UTC).timestamp())
                from io import BytesIO
                tar.addfile(info, BytesIO(payload))

            _add_bytes(
                "MANIFEST.json",
                json.dumps(manifest, indent=2).encode("utf-8"),
            )
            _add_bytes(
                "classroom.json",
                json.dumps(classroom, indent=2).encode("utf-8"),
            )
            _add_bytes(
                "course.json",
                json.dumps(course, indent=2).encode("utf-8"),
            )
            if cohort_path.is_file():
                tar.add(cohort_path, arcname="cohort.json")
            if coord_classroom_dir.is_dir():
                tar.add(coord_classroom_dir, arcname="coordinator")
    except OSError as exc:
        return {"exported": False, "error": f"write failed: {exc}"}

    return {
        "exported": True,
        "path": str(out),
        "classroom_id": classroom_id,
        "exported_at": exported_at,
    }


def harvest_classroom(
    *, classroom_id: str, out_path: Path | str,
) -> dict[str, Any]:
    """Bundle a classroom's cohort data into a ``.axiompack`` zip for export.

    Principal ids are pseudonymized via
    ``axiom.medallion.export.pseudonymize`` (deterministic hash-based —
    longitudinal joins across bundles stay stable). Student names and
    emails are redacted.

    Contents:
      - ``MANIFEST.yaml`` — bundle metadata (classroom id, harvested
        timestamp, anonymization note).
      - ``classroom.json`` — anonymized classroom record.
      - ``course.json`` — course record (no student PII).
      - ``grades.jsonl`` — one row per graded response, with
        pseudonymized student ids (empty file when no ledger exists).
      - ``README.md`` — human-readable orientation for researchers.

    Returns ``{"harvested": bool, "path"?: str, "error"?: str}``.
    """
    import zipfile
    from datetime import UTC, datetime

    import yaml

    from axiom.medallion.export import pseudonymize

    from .operational_store import load_classroom_data, load_course_data

    out = Path(out_path)

    classroom = load_classroom_data(classroom_id)
    if classroom is None:
        return {
            "harvested": False,
            "error": f"classroom {classroom_id!r} not found",
        }

    course_id = classroom.get("course_id", "")
    course = load_course_data(course_id) if course_id else {}

    # --- Anonymize roster -------------------------------------------------
    anon_classroom = _anonymize_classroom(classroom, pseudonymize)

    # --- Grade ledger → JSONL with pseudonymized student ids -------------
    ledger_rows = _grade_ledger_to_jsonl_rows(classroom_id, pseudonymize)

    # --- MANIFEST + README ------------------------------------------------
    harvested_at = datetime.now(UTC).isoformat()
    manifest = {
        "bundle": "classroom-harvest",
        "classroom_id": classroom_id,
        "course_id": course_id,
        "title": classroom.get("title", ""),
        "harvested_at": harvested_at,
        "anonymization": (
            "Student principals pseudonymized via SHA-256; names + "
            "emails redacted. Pseudonyms are deterministic across "
            "harvests of this cohort."
        ),
        "contents": [
            "MANIFEST.yaml",
            "classroom.json",
            "course.json",
            "grades.jsonl",
            "README.md",
        ],
    }

    readme = (
        f"# Classroom harvest — {classroom_id}\n\n"
        f"Harvested: {harvested_at}\n\n"
        f"This bundle contains anonymized cohort data from the classroom\n"
        f"named ``{classroom.get('title', classroom_id)}``. Student\n"
        f"principals are pseudonymized; names and emails are redacted.\n\n"
        f"## Files\n\n"
        f"- ``MANIFEST.yaml`` — bundle metadata\n"
        f"- ``classroom.json`` — classroom record (anonymized)\n"
        f"- ``course.json`` — course record\n"
        f"- ``grades.jsonl`` — per-response grades with pseudonymized IDs\n"
    )

    out.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("MANIFEST.yaml", yaml.safe_dump(manifest, default_flow_style=False))
        zf.writestr("classroom.json", json.dumps(anon_classroom, indent=2))
        zf.writestr("course.json", json.dumps(course or {}, indent=2))
        zf.writestr(
            "grades.jsonl",
            "\n".join(json.dumps(row) for row in ledger_rows),
        )
        zf.writestr("README.md", readme)

    return {
        "harvested": True,
        "classroom_id": classroom_id,
        "path": str(out),
        "harvested_at": harvested_at,
    }


def _anonymize_classroom(
    data: dict[str, Any], pseudonymize: Any,
) -> dict[str, Any]:
    """Strip PII from a classroom record while preserving structure."""
    out = dict(data)
    anon_roster = []
    for s in data.get("lms_roster") or []:
        sid = s.get("id", "")
        anon_roster.append(
            {
                "id": pseudonymize(sid) if sid else "",
                "name": "<redacted>",
                "email": "<redacted>",
                "principal": pseudonymize(sid) if sid else "",
            }
        )
    out["lms_roster"] = anon_roster
    # Archiver / published_by are instructor principals, not student PII;
    # keep them as-is so audit records remain meaningful.
    return out


def _grade_ledger_to_jsonl_rows(
    classroom_id: str, pseudonymize: Any,
) -> list[dict[str, Any]]:
    """Flatten the grade ledger into anonymized JSONL rows."""
    ledger = _read_grade_ledger(classroom_id)
    rows: list[dict[str, Any]] = []
    for entry in ledger:
        assessment_id = entry.get("assessment_id", "")
        for g in entry.get("grades") or []:
            sid = g.get("student_id", "")
            if not sid:
                continue
            rows.append(
                {
                    "assessment_id": assessment_id,
                    "student_pseudonym": pseudonymize(sid),
                    "score": g.get("score"),
                }
            )
    return rows


def compute_final_grades(classroom_id: str) -> dict[str, Any]:
    """Compute per-student final grades from the grade ledger.

    v0 formula: equal-weight mean across all graded assessments for
    each student. Rubric-weighted means + instructor-configurable
    formulas land in v1. Returns:

        {
            "classroom_id": "...",
            "formula": "equal_weight_mean",
            "students": [
                {"student_id": ..., "final_grade": ..., "assessments_graded": N,
                 "scores": {assessment_id: score, ...}},
                ...
            ],
        }

    Error form: ``{"error": "..."}``.
    """
    from .operational_store import load_classroom_data

    if load_classroom_data(classroom_id) is None:
        return {"error": f"classroom {classroom_id!r} not found"}

    ledger = _read_grade_ledger(classroom_id)
    per_student: dict[str, dict[str, float]] = {}
    for entry in ledger:
        aid = entry.get("assessment_id", "")
        for g in entry.get("grades") or []:
            sid = g.get("student_id")
            score = g.get("score")
            if not sid or not isinstance(score, (int, float)):
                continue
            per_student.setdefault(sid, {})[aid] = float(score)

    students = []
    for sid, scores in sorted(per_student.items()):
        final = sum(scores.values()) / len(scores)
        students.append(
            {
                "student_id": sid,
                "assessments_graded": len(scores),
                "scores": scores,
                "final_grade": round(final, 4),
            }
        )

    return {
        "classroom_id": classroom_id,
        "formula": "equal_weight_mean",
        "students": students,
    }


def finalize_grades(
    *,
    classroom_id: str,
    push: bool = False,
    canvas_course_id: str | None = None,
    canvas_assignment_id: str | None = None,
    provider: Any = None,
) -> dict[str, Any]:
    """Compute final grades and optionally push to Canvas.

    Args:
        classroom_id: Classroom to finalize.
        push: If True, push each student's final to Canvas via
            ``provider.push_grade``. Default False (compute-only).
        canvas_course_id: Target Canvas course id. Falls back to
            the classroom's stored ``lms_course_id`` from P4 setup.
        canvas_assignment_id: Target Canvas assignment id for the
            final grade. No fallback — required if push=True.
        provider: LMSProvider instance. Falls back to
            ``build_lms_provider_from_env()`` if not supplied.

    Returns a dict with ``students``, ``pushed``, ``failures``
    (when push=True), or ``error`` on input problems.
    """
    from .operational_store import load_classroom_data

    computed = compute_final_grades(classroom_id)
    if "error" in computed:
        return computed

    students = computed["students"]
    result: dict[str, Any] = {
        "classroom_id": classroom_id,
        "formula": computed["formula"],
        "students": students,
        "pushed": False,
        "failures": [],
    }

    if not push:
        return result

    if not canvas_assignment_id:
        result["error"] = (
            "canvas_assignment_id is required for push=True"
        )
        return result

    # Resolve course id if not supplied
    if not canvas_course_id:
        classroom_data = load_classroom_data(classroom_id) or {}
        canvas_course_id = classroom_data.get("lms_course_id", "")
        if not canvas_course_id:
            result["error"] = (
                "canvas_course_id is required (not set on classroom and "
                "not passed explicitly)"
            )
            return result

    # Resolve provider if not supplied
    if provider is None:
        from axiom.integrations.lms.env import build_lms_provider_from_env

        provider = build_lms_provider_from_env()
        if provider is None:
            result["error"] = (
                "no LMS provider configured; set AXIOM_CANVAS_API_URL + "
                "AXIOM_CANVAS_API_TOKEN or pass provider= explicitly"
            )
            return result

    result["pushed"] = True
    for s in students:
        try:
            push_result = provider.push_grade(
                course_id=canvas_course_id,
                assignment_id=canvas_assignment_id,
                student_id=s["student_id"],
                score=s["final_grade"],
                comment=(
                    f"Final grade (v0 equal-weight mean over "
                    f"{s['assessments_graded']} assessment(s))."
                ),
            )
        except Exception as e:
            result["failures"].append(
                {"student_id": s["student_id"], "error": f"exception: {e}"}
            )
            continue
        if not getattr(push_result, "success", False):
            result["failures"].append(
                {
                    "student_id": s["student_id"],
                    "error": getattr(push_result, "message", "unknown"),
                }
            )

    return result


def propose_template_update(
    *, classroom_id: str,
) -> dict[str, Any]:
    """Propose an updated CourseManifest for the next cohort.

    Advisory only — returns a ``{proposed_manifest, rationale}``
    payload the instructor reviews. Nothing is auto-applied.

    Signals v0 looks for (grades-only; trace signals come later):

    - ``low_mean`` — assessment whose cohort mean is < 0.7. Rationale
      suggests adding a pre-checkpoint rail / study-guide prompt
      so students arrive better prepared.
    - ``high_variance`` — assessment whose cohort stdev > 0.2. The
      rubric may need clarification or the assessment itself may be
      ambiguous.
    - ``failures_present`` — at least one student scored < 0.6 on
      any assessment. Proposal adds a ``retake`` checkpoint so the
      next cohort's struggling students have a recovery path.

    Healthy cohorts (all means ≥ 0.7, stdev ≤ 0.2, no failures)
    produce a proposal that mirrors the existing manifest with no
    flagged rationale — the instructor can still choose to ship a
    fresh version.
    """
    from copy import deepcopy

    from .operational_store import load_classroom_data, load_course_data

    classroom = load_classroom_data(classroom_id)
    if classroom is None:
        return {"error": f"classroom {classroom_id!r} not found"}

    course_id = classroom.get("course_id", "")
    course = load_course_data(course_id) if course_id else {}
    manifest = deepcopy((course or {}).get("manifest") or {})

    ledger = _read_grade_ledger(classroom_id)
    agg = _aggregate_grade_ledger(ledger)

    rationale: list[dict[str, Any]] = []
    any_failure = False

    for a in agg.get("assessments") or []:
        aid = a.get("assessment_id")
        mean = a.get("mean", 1.0)
        stdev = a.get("stdev", 0.0)
        if mean < 0.7:
            rationale.append(
                {
                    "assessment_id": aid,
                    "signal": "low_mean",
                    "mean": mean,
                    "suggestion": (
                        f"Cohort mean on {aid} was {mean:.2f}. Consider "
                        "adding a pre-checkpoint onboarding rail that "
                        "surfaces the assessment's core concepts, or "
                        "augment the corpus with targeted study guides."
                    ),
                }
            )
        if stdev > 0.2:
            rationale.append(
                {
                    "assessment_id": aid,
                    "signal": "high_variance",
                    "stdev": stdev,
                    "suggestion": (
                        f"Score spread on {aid} was σ={stdev:.2f}. "
                        "Review the rubric for clarity and the question "
                        "prompts for ambiguity."
                    ),
                }
            )
        # Check for individual failures via the raw ledger (agg only has
        # aggregate stats).
        for entry in ledger:
            if entry.get("assessment_id") != aid:
                continue
            for g in entry.get("grades") or []:
                score = g.get("score")
                if isinstance(score, (int, float)) and score < 0.6:
                    any_failure = True
                    break

    if any_failure:
        rationale.append(
            {
                "signal": "failures_present",
                "suggestion": (
                    "Some students scored below 0.6 on at least one "
                    "checkpoint. Proposal adds a ``retake`` checkpoint "
                    "near course_end so next cohort's struggling "
                    "students have a recovery path."
                ),
            }
        )
        checkpoints = manifest.setdefault("checkpoints", [])
        if not any(cp.get("id") == "retake" for cp in checkpoints):
            checkpoints.append(
                {
                    "id": "retake",
                    "label": "Retake (added by P5 proposal)",
                    "timing": "course_end",
                    "method": "quiz",
                    "questionnaire_id": "",
                    "required": False,
                }
            )

    return {
        "classroom_id": classroom_id,
        "proposed_manifest": manifest,
        "rationale": rationale,
    }


def format_summary_markdown(summary: dict[str, Any]) -> str:
    """Render a cohort summary as a human-readable markdown block."""
    if "error" in summary:
        return f"Error: {summary['error']}"

    roster = summary.get("roster") or {}
    cfg = summary.get("course_config") or {}
    grades = summary.get("grade_ledger") or {}

    lines = [
        f"# Cohort summary — {summary.get('classroom_id', '(unknown)')}",
        "",
        f"- Title: {summary.get('title') or '(untitled)'}",
        f"- Course: {summary.get('course_id') or '(none)'}",
        f"- State: {summary.get('state', '(unknown)')}",
    ]
    if summary.get("published_at"):
        lines.append(f"- Published: {summary['published_at']}")
    if summary.get("archived_at"):
        lines.append(f"- Archived: {summary['archived_at']}")

    lines.extend(
        [
            "",
            "## Roster",
            f"- Size: {roster.get('size', 0)}",
            "",
            "## Course configuration",
            f"- Checkpoints: {cfg.get('checkpoints', 0)}",
            f"- Assessments: {cfg.get('assessments', 0)}",
            f"- Onboarding rails: {cfg.get('rails', 0)}",
            f"- System prompt set: {'yes' if cfg.get('has_system_prompt') else 'no'}",
            "",
            "## Grades",
            f"- Total graded responses: {grades.get('total_graded', 0)}",
        ]
    )
    for a in grades.get("assessments") or []:
        lines.append(
            f"  - **{a['assessment_id']}** — n={a['count']}, "
            f"mean={a['mean']:.3f}, σ={a['stdev']:.3f}, "
            f"range=[{a['min']:.3f}, {a['max']:.3f}]"
        )
    return "\n".join(lines)
