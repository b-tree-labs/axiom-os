# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Per-classroom self-diagnostic.

`axi classroom doctor <CID>` walks the invariants the rest of the
classroom CLI silently assumes: identity exists, the operational store
has a course + classroom record, the coordinator-side stores are
populated, and so on. Each check returns a structured ``CheckResult``
with a status (``ok`` | ``warn`` | ``fail``) and a one-line ``hint``
that names the exact next command if the check fails.

The doctor is intentionally read-only — it never mutates state. A
follow-up ``--fix`` flag could auto-repair specific failures, but for
v0 the diagnostic surface is enough: tell the user what's wrong, hand
them the exact command, let them act.

Two diagnostic modes drive the check selection:

- ``instructor`` — the local node has a coordinator-side classroom
  directory; checks course + classroom artifacts, materials store,
  cohort registry, identity.
- ``student`` — the local node has a membership manifest for the
  classroom; checks the membership, coordinator URL sidecar, local
  materials index, server reachability.

When neither side is present locally, the doctor reports ``unknown``
role and points the user at ``axi classroom prep init`` (instructor)
or ``axi classroom join`` (student) as the appropriate entry point.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Result model
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CheckResult:
    """One diagnostic finding.

    ``status`` is one of:
      ``ok``   — invariant holds.
      ``warn`` — invariant fails but is non-fatal (e.g., demo
                 materials missing on a freshly-cloned classroom).
      ``fail`` — invariant fails and a downstream command will
                 break unless the user takes action.

    ``hint`` carries the recommended next step and (when applicable)
    the exact command to run. Keep hints short — one line, no jargon.
    """

    name: str
    status: str  # "ok" | "warn" | "fail"
    detail: str
    hint: str = ""


@dataclass
class DiagnosticReport:
    classroom_id: str
    role: str  # "instructor" | "student" | "unknown"
    checks: list[CheckResult] = field(default_factory=list)

    @property
    def overall(self) -> str:
        if any(c.status == "fail" for c in self.checks):
            return "fail"
        if any(c.status == "warn" for c in self.checks):
            return "warn"
        return "ok"

    def to_dict(self) -> dict[str, Any]:
        return {
            "classroom_id": self.classroom_id,
            "role": self.role,
            "overall": self.overall,
            "checks": [asdict(c) for c in self.checks],
        }


# ---------------------------------------------------------------------------
# Role detection (mirrors cli._detect_classroom_role to keep doctor
# importable without pulling in cli.py's argparse machinery)
# ---------------------------------------------------------------------------


def detect_role(classroom_id: str) -> str:
    coord_dir = (
        Path.home() / ".axi" / "coordinator" / "classrooms" / classroom_id
    )
    if coord_dir.is_dir():
        return "instructor"
    student_dir = Path.home() / ".axi" / "classrooms" / classroom_id
    if (student_dir / "membership.json").is_file():
        return "student"
    return "unknown"


# ---------------------------------------------------------------------------
# Top-level entry point
# ---------------------------------------------------------------------------


def run_diagnostics(classroom_id: str) -> DiagnosticReport:
    """Run every applicable check for the given classroom.

    Selects checks based on the locally-detected role. Always runs at
    least one check (the role-detection check itself) so the report
    is never empty.
    """
    role = detect_role(classroom_id)
    report = DiagnosticReport(classroom_id=classroom_id, role=role)

    if role == "unknown":
        report.checks.append(CheckResult(
            name="local_role",
            status="fail",
            detail=(
                f"No local state for classroom {classroom_id!r}. "
                "Neither a coordinator directory (instructor) nor a "
                "membership manifest (student) exists on this machine."
            ),
            hint=(
                "If you're the instructor: `axi classroom prep init "
                "--title \"...\" --instructor <you>`. "
                "If you're a student: `axi classroom join <invite>`."
            ),
        ))
        return report

    report.checks.append(CheckResult(
        name="local_role",
        status="ok",
        detail=f"Detected role: {role}",
    ))

    if role == "instructor":
        _run_instructor_checks(classroom_id, report)
    elif role == "student":
        _run_student_checks(classroom_id, report)

    return report


# ---------------------------------------------------------------------------
# Instructor-side checks
# ---------------------------------------------------------------------------


def _run_instructor_checks(
    classroom_id: str, report: DiagnosticReport,
) -> None:
    _check_identity(report)
    _check_classroom_artifact(classroom_id, report)
    _check_course_artifact(classroom_id, report)
    _check_cohort_registry(classroom_id, report)
    _check_coordinator_materials(classroom_id, report)
    _check_classroom_state(classroom_id, report)


def _check_identity(report: DiagnosticReport) -> None:
    try:
        from axiom.vega.federation.identity import load_identity
    except ImportError:
        report.checks.append(CheckResult(
            name="identity",
            status="fail",
            detail="federation identity module not importable",
            hint="reinstall axiom: `pip install --upgrade axiom`",
        ))
        return

    identity = load_identity()
    if identity is None:
        report.checks.append(CheckResult(
            name="identity",
            status="warn",
            detail="No node identity on disk yet.",
            hint=(
                "First `axi classroom invite` or `axi classroom join` "
                "will auto-init it with one-line narration."
            ),
        ))
        return
    report.checks.append(CheckResult(
        name="identity",
        status="ok",
        detail=f"Node identity present (owner={identity.owner})",
    ))


def _check_classroom_artifact(
    classroom_id: str, report: DiagnosticReport,
) -> None:
    from .operational_store import load_classroom_data

    data = load_classroom_data(classroom_id)
    if data is None:
        report.checks.append(CheckResult(
            name="classroom_artifact",
            status="fail",
            detail=(
                f"No operational record for classroom {classroom_id!r}. "
                "Coordinator-side state exists but the classroom artifact "
                "is missing — likely a partial cleanup."
            ),
            hint="`axi classroom prep init --title \"...\" --instructor <you>` to recreate.",
        ))
        return
    state = data.get("state") or "unpublished"
    detail = f"state={state}"
    if state == "archived":
        report.checks.append(CheckResult(
            name="classroom_artifact",
            status="warn",
            detail=detail + " (terminal — clone for a new cohort)",
            hint=f"`axi classroom prep from-demo --from {classroom_id}`",
        ))
        return
    report.checks.append(CheckResult(
        name="classroom_artifact", status="ok", detail=detail,
    ))


def _check_course_artifact(
    classroom_id: str, report: DiagnosticReport,
) -> None:
    from .operational_store import load_classroom_data, load_course_data

    classroom = load_classroom_data(classroom_id)
    if classroom is None:
        return  # already flagged in classroom_artifact check
    course_id = classroom.get("course_id") or ""
    if not course_id:
        report.checks.append(CheckResult(
            name="course_artifact",
            status="fail",
            detail="Classroom has no course_id — checklist may be stuck.",
            hint=f"`axi classroom prep status {classroom_id}` (self-heals course_selected)",
        ))
        return
    course = load_course_data(course_id)
    if course is None:
        report.checks.append(CheckResult(
            name="course_artifact",
            status="fail",
            detail=(
                f"Course {course_id!r} referenced by this classroom is "
                "missing from the operational store."
            ),
            hint="`axi classroom prep init --title \"...\" --instructor <you>` to recreate.",
        ))
        return
    report.checks.append(CheckResult(
        name="course_artifact",
        status="ok",
        detail=f"course_id={course_id}",
    ))


def _check_cohort_registry(
    classroom_id: str, report: DiagnosticReport,
) -> None:
    from .coordinator_cohort_store import (
        CohortNotFoundError,
        FileCohortStore,
    )

    coord_dir = Path.home() / ".axi" / "coordinator"
    store = FileCohortStore(coord_dir)
    if not store.exists(classroom_id):
        report.checks.append(CheckResult(
            name="cohort_registry",
            status="warn",
            detail="No cohort entry for this classroom yet.",
            hint=f"`axi classroom invite {classroom_id} --coordinator-url <url>` to mint the first invite (auto-creates the cohort).",
        ))
        return
    try:
        url = store.get_coordinator_url(classroom_id)
    except (CohortNotFoundError, ValueError):
        url = None
    member_count = 0
    try:
        cohort = store.load(classroom_id)
        member_count = len(getattr(cohort, "members", []))
    except CohortNotFoundError:
        pass
    detail = f"members={member_count}, url={url or '(none)'}"
    if not url:
        report.checks.append(CheckResult(
            name="cohort_registry",
            status="warn",
            detail=detail,
            hint=f"Set the coordinator URL: `axi classroom invite {classroom_id} --coordinator-url <url>`",
        ))
        return
    report.checks.append(CheckResult(
        name="cohort_registry", status="ok", detail=detail,
    ))


def _check_coordinator_materials(
    classroom_id: str, report: DiagnosticReport,
) -> None:
    from .classroom_materials import ClassroomMaterialsStore

    coord_dir = (
        Path.home() / ".axi" / "coordinator"
        / "classrooms" / classroom_id
    )
    if not coord_dir.is_dir():
        report.checks.append(CheckResult(
            name="coordinator_materials",
            status="warn",
            detail="No coordinator materials directory yet.",
            hint=f"`axi classroom prep corpus {classroom_id} --upload <file>` to seed.",
        ))
        return
    materials = ClassroomMaterialsStore(coord_dir)
    entries = materials.list_entries()
    if not entries:
        report.checks.append(CheckResult(
            name="coordinator_materials",
            status="warn",
            detail="Materials store exists but no files registered.",
            hint=f"`axi classroom prep corpus {classroom_id} --upload <file>`",
        ))
        return
    report.checks.append(CheckResult(
        name="coordinator_materials",
        status="ok",
        detail=f"{len(entries)} file(s) registered",
    ))


def _check_classroom_state(
    classroom_id: str, report: DiagnosticReport,
) -> None:
    """Higher-level publishability check: course publishable AND
    classroom ready. Catches the stuck `course_selected` case before
    the user runs into it."""
    from .operational_store import (
        load_classroom,
        load_classroom_data,
        load_course,
    )

    loaded_classroom = load_classroom(classroom_id)
    if loaded_classroom is None:
        return
    classroom_wf, classroom_data = loaded_classroom
    course_id = classroom_data.get("course_id") or ""
    if not course_id:
        return
    loaded_course = load_course(course_id)
    if loaded_course is None:
        return
    course_wf, _ = loaded_course

    course_ready, course_blockers = course_wf.is_ready_to_publish()
    class_ready, class_blockers = classroom_wf.is_ready_for_enrollment()

    if course_ready and class_ready:
        state = (load_classroom_data(classroom_id) or {}).get("state")
        published = state == "published"
        report.checks.append(CheckResult(
            name="prep_publishable",
            status="ok",
            detail="published" if published else "ready to publish",
            hint=(
                "" if published
                else f"`axi classroom publish {classroom_id} --approver <you>`"
            ),
        ))
        return

    blockers = []
    if not course_ready:
        blockers.extend(f"course:{b}" for b in course_blockers)
    if not class_ready:
        blockers.extend(f"classroom:{b}" for b in class_blockers)
    report.checks.append(CheckResult(
        name="prep_publishable",
        status="warn",
        detail=f"blockers: {', '.join(blockers) or 'unknown'}",
        hint=f"`axi classroom prep status {classroom_id}` for the full checklist.",
    ))


# ---------------------------------------------------------------------------
# Student-side checks
# ---------------------------------------------------------------------------


def _run_student_checks(
    classroom_id: str, report: DiagnosticReport,
) -> None:
    student_dir = Path.home() / ".axi" / "classrooms" / classroom_id
    _check_membership_manifest(classroom_id, student_dir, report)
    _check_coordinator_url_sidecar(classroom_id, student_dir, report)
    _check_local_materials_index(classroom_id, student_dir, report)
    _check_coordinator_reachable(classroom_id, student_dir, report)


def _check_membership_manifest(
    classroom_id: str, student_dir: Path, report: DiagnosticReport,
) -> None:
    from .student_membership import (
        MembershipNotFoundError,
        MembershipStore,
    )

    store = MembershipStore(base_dir=Path.home() / ".axi")
    try:
        stored = store.load(classroom_id)
    except MembershipNotFoundError as exc:
        report.checks.append(CheckResult(
            name="membership",
            status="fail",
            detail=f"Membership manifest missing or invalid: {exc}",
            hint="`axi classroom join <invite-from-your-instructor>`",
        ))
        return
    report.checks.append(CheckResult(
        name="membership",
        status="ok",
        detail=f"student_id={stored.student_id}",
    ))


def _check_coordinator_url_sidecar(
    classroom_id: str, student_dir: Path, report: DiagnosticReport,
) -> None:
    sidecar = student_dir / "coordinator_url.txt"
    if not sidecar.is_file():
        report.checks.append(CheckResult(
            name="coordinator_url",
            status="warn",
            detail="No coordinator URL sidecar — fresh-data fetches will fail.",
            hint=(
                "Re-join the class with the same invite to repair: "
                "`axi classroom join <invite>`."
            ),
        ))
        return
    url = sidecar.read_text().strip()
    if not url:
        report.checks.append(CheckResult(
            name="coordinator_url",
            status="warn",
            detail="Coordinator URL sidecar is empty.",
            hint="Re-join the class to repair.",
        ))
        return
    report.checks.append(CheckResult(
        name="coordinator_url",
        status="ok",
        detail=f"url={url}",
    ))


def _check_local_materials_index(
    classroom_id: str, student_dir: Path, report: DiagnosticReport,
) -> None:
    from .classroom_local_index import ClassroomLocalIndex

    index = ClassroomLocalIndex(base_dir=student_dir)
    index.open()
    try:
        chunks = index.chunk_count()
    finally:
        index.close()
    if chunks == 0:
        report.checks.append(CheckResult(
            name="local_index",
            status="warn",
            detail="No materials indexed locally — `ask` will return no passages.",
            hint=(
                "Materials sync runs at join time. Re-join (or wait for "
                "instructor to upload + republish if you joined early)."
            ),
        ))
        return
    report.checks.append(CheckResult(
        name="local_index",
        status="ok",
        detail=f"{chunks} chunk(s) in local index",
    ))


def _check_coordinator_reachable(
    classroom_id: str, student_dir: Path, report: DiagnosticReport,
) -> None:
    sidecar = student_dir / "coordinator_url.txt"
    if not sidecar.is_file():
        return  # already flagged in coordinator_url check
    base = sidecar.read_text().strip().rstrip("/")
    # base might end with /classroom/join — strip for healthz.
    if base.endswith("/classroom/join"):
        base = base[: -len("/classroom/join")]

    import urllib.error
    import urllib.request

    target = base + "/healthz"
    try:
        with urllib.request.urlopen(target, timeout=2.0) as resp:
            ok = resp.status == 200
            body = resp.read().decode("utf-8", errors="replace")
    except (urllib.error.URLError, urllib.error.HTTPError, OSError) as exc:
        report.checks.append(CheckResult(
            name="coordinator_reachable",
            status="warn",
            detail=f"Couldn't reach {target}: {exc}",
            hint=(
                "If the instructor isn't running `axi classroom serve`, "
                "this is expected. Otherwise, check network."
            ),
        ))
        return
    if not ok:
        report.checks.append(CheckResult(
            name="coordinator_reachable",
            status="warn",
            detail=f"Coordinator returned non-200 at {target}",
            hint="Ask your instructor whether the classroom server is running.",
        ))
        return
    try:
        data = json.loads(body) if body else {}
        cid = data.get("classroom_id") if isinstance(data, dict) else None
    except json.JSONDecodeError:
        cid = None
    detail = "healthz ok"
    if cid and cid != classroom_id:
        detail += f" (note: server reports class={cid!r})"
    report.checks.append(CheckResult(
        name="coordinator_reachable",
        status="ok",
        detail=detail,
    ))


__all__ = [
    "CheckResult",
    "DiagnosticReport",
    "detect_role",
    "run_diagnostics",
]
