# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""FW-1 P1 — seeded demo classroom for skeptic-evaluation-in-60s.

``axi classroom demo`` creates a fully-populated course + classroom in
the operational store so a first-time instructor sees a running
classroom before touching any configuration. Per prd-classroom §2.5
(enter through the end), the first meaningful interaction is with a
working end product, not with a blank form.

Everything is inline Python data — no network calls, no external
corpus download, no LLM required for the seeding itself. The course is
deliberately domain-agnostic (classical mechanics) so classroom core
ships without naming any consumer domain (per
feedback_axiom_domain_agnostic). Consumer layers can ship their own
themed demos via extension hooks in later phases.

Public API:

- ``seed_demo()`` — idempotent; writes course + classroom artifacts.
- ``reset_demo()`` — delete existing demo artifacts then reseed.
- ``clone_demo_course(new_course_id, instructor_id)`` — copy the
  demo course into a fresh editable course under a new id. The demo
  artifacts themselves are never modified by this call.
"""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from .course_prep_workflow import CoursePrepWorkflow

# ---------------------------------------------------------------------------
# Demo identity
# ---------------------------------------------------------------------------

DEMO_COURSE_ID = "demo-classical-mechanics"
DEMO_CLASSROOM_ID = "demo-classical-mechanics-spring"
DEMO_COURSE_SLUG = "demo-classical-mechanics"
DEMO_CLASSROOM_SLUG = "demo-classical-mechanics-spring"
DEMO_INSTRUCTOR_ID = "@instructor:demo"
DEMO_TITLE = "Classical Mechanics 101 (Demo)"


# ---------------------------------------------------------------------------
# Inline content — corpus, roster, assessments, rails, prompt
# ---------------------------------------------------------------------------

DEMO_CORPUS: list[dict[str, Any]] = [
    {
        "id": "doc-1-newton-first",
        "title": "Newton's First Law",
        "text": (
            "An object at rest remains at rest, and an object in motion "
            "remains in motion at constant velocity, unless acted upon by "
            "a net external force. This principle of inertia was first "
            "clearly stated by Galileo and later formalized by Newton."
        ),
    },
    {
        "id": "doc-2-newton-second",
        "title": "Newton's Second Law",
        "text": (
            "The net force on an object is equal to the product of its "
            "mass and its acceleration: F = m * a. This relationship is "
            "the foundation of classical dynamics. Mass is a measure of "
            "inertia; larger masses require proportionally larger forces "
            "to produce the same acceleration."
        ),
    },
    {
        "id": "doc-3-newton-third",
        "title": "Newton's Third Law",
        "text": (
            "For every action there is an equal and opposite reaction. "
            "When body A exerts a force on body B, body B simultaneously "
            "exerts a force on body A with equal magnitude and opposite "
            "direction. Action-reaction pairs act on different bodies."
        ),
    },
    {
        "id": "doc-4-kinematics",
        "title": "Kinematic Equations",
        "text": (
            "Kinematics describes motion without reference to forces. The "
            "core kinematic quantities are position, velocity, and "
            "acceleration. For uniform acceleration: v = v0 + a*t, and "
            "x = x0 + v0*t + 0.5*a*t^2."
        ),
    },
    {
        "id": "doc-5-energy",
        "title": "Work and Kinetic Energy",
        "text": (
            "Work done by a constant force is W = F * d * cos(theta), "
            "where theta is the angle between force and displacement. "
            "The work-energy theorem states that the net work done on "
            "an object equals its change in kinetic energy: W_net = dKE."
        ),
    },
    {
        "id": "doc-6-potential-energy",
        "title": "Potential Energy",
        "text": (
            "Potential energy is stored energy associated with position "
            "in a force field. Gravitational potential energy near "
            "Earth's surface is U = m*g*h. Elastic potential energy in a "
            "spring is U = 0.5*k*x^2, where k is the spring constant."
        ),
    },
    {
        "id": "doc-7-momentum",
        "title": "Linear Momentum",
        "text": (
            "Linear momentum is p = m * v. In an isolated system (no "
            "external forces), total momentum is conserved. Conservation "
            "of momentum is a direct consequence of Newton's third law "
            "and applies to collisions, explosions, and many-body systems."
        ),
    },
    {
        "id": "doc-8-circular-motion",
        "title": "Uniform Circular Motion",
        "text": (
            "An object in uniform circular motion has constant speed but "
            "continuously changing velocity direction. The centripetal "
            "acceleration points toward the center and has magnitude "
            "a = v^2 / r. The required centripetal force is F = m*v^2/r."
        ),
    },
    {
        "id": "doc-9-oscillations",
        "title": "Simple Harmonic Motion",
        "text": (
            "Simple harmonic motion arises when the restoring force on "
            "an object is proportional to its displacement from equilibrium: "
            "F = -k*x. The period of oscillation for a mass-spring system "
            "is T = 2*pi*sqrt(m/k), independent of amplitude for small "
            "oscillations."
        ),
    },
    {
        "id": "doc-10-gravitation",
        "title": "Universal Gravitation",
        "text": (
            "Newton's law of universal gravitation states that every "
            "particle attracts every other particle with a force "
            "proportional to the product of their masses and inversely "
            "proportional to the square of the distance between them: "
            "F = G * m1 * m2 / r^2."
        ),
    },
]


DEMO_ROSTER: list[dict[str, Any]] = [
    {"id": "s-alice", "name": "Alice", "email": "alice@demo.local", "principal": "@alice:demo"},
    {"id": "s-bob", "name": "Bob", "email": "bob@demo.local", "principal": "@bob:demo"},
    {"id": "s-carol", "name": "Carol", "email": "carol@demo.local", "principal": "@carol:demo"},
    {"id": "s-dave", "name": "Dave", "email": "dave@demo.local", "principal": "@dave:demo"},
    {"id": "s-erin", "name": "Erin", "email": "erin@demo.local", "principal": "@erin:demo"},
]


DEMO_ASSESSMENTS: list[dict[str, Any]] = [
    {
        "id": "baseline",
        "label": "Baseline Assessment",
        "timing": "enrollment_complete",
        "method": "quiz",
        "required": True,
        "questions": [
            {
                "id": "q1",
                "prompt": "State Newton's second law and explain each term.",
                "model_answer": (
                    "F = m*a. F is the net external force (vector), m is "
                    "the object's mass (scalar), a is the acceleration "
                    "(vector) in the direction of the net force."
                ),
                "rubric": ["states equation", "defines each symbol", "notes vector nature"],
            },
            {
                "id": "q2",
                "prompt": "What does the work-energy theorem say?",
                "model_answer": (
                    "The net work done on an object equals its change in "
                    "kinetic energy: W_net = dKE."
                ),
                "rubric": ["states theorem", "identifies work and kinetic energy"],
            },
        ],
    },
    {
        "id": "midpoint",
        "label": "Midpoint Assessment",
        "timing": "midway",
        "method": "quiz",
        "required": True,
        "questions": [
            {
                "id": "q1",
                "prompt": (
                    "A 2 kg object on a frictionless surface is pushed with "
                    "a constant 10 N force for 5 seconds. What is its final "
                    "velocity if starting from rest?"
                ),
                "model_answer": (
                    "a = F/m = 10/2 = 5 m/s^2. v = v0 + a*t = 0 + 5*5 = 25 m/s."
                ),
                "rubric": ["computes acceleration", "applies kinematics", "correct numerical answer"],
            },
            {
                "id": "q2",
                "prompt": (
                    "Explain why momentum is conserved in an isolated system."
                ),
                "model_answer": (
                    "By Newton's third law, internal forces cancel pairwise. "
                    "With no external force, the net force on the system is "
                    "zero, so d(total momentum)/dt = 0 — total momentum is "
                    "constant in time."
                ),
                "rubric": ["invokes Newton's third law", "internal forces cancel", "conclusion"],
            },
        ],
    },
]


DEMO_RAILS: list[dict[str, Any]] = [
    {
        "id": "pre-course-interview",
        "source": "axiom-core",
        "auto_apply_to": "all_new_students",
        "required": True,
        "questions": [
            {
                "id": "prior-experience",
                "prompt": "Have you taken a physics course before?",
                "response_type": "yes_no",
            },
            {
                "id": "confidence-calculus",
                "prompt": "On a 1–5 scale, how comfortable are you with calculus?",
                "response_type": "likert",
            },
        ],
    },
]


DEMO_SYSTEM_PROMPT = (
    "You are a Classical Mechanics 101 teaching assistant. You ground "
    "every answer in the course corpus and cite the source document "
    "using inline [C#] markers. If a student's question falls outside "
    "the corpus, say so plainly and suggest the closest relevant topic. "
    "Never fabricate equations or physical constants."
)


DEMO_MANIFEST: dict[str, Any] = {
    "id": DEMO_COURSE_ID,
    "title": DEMO_TITLE,
    "version": "1.0.0",
    "learning_objectives": [
        "Apply Newton's three laws to solve mechanics problems.",
        "Use the work-energy theorem to analyze motion under variable forces.",
        "Use conservation of momentum to analyze collisions.",
        "Connect kinematic and dynamical descriptions of motion.",
    ],
}


def _demo_manifest_with_defaults() -> dict[str, Any]:
    """Return a copy of DEMO_MANIFEST with default checkpoints injected."""
    from .checkpoints import apply_default_checkpoints

    out = {k: (list(v) if isinstance(v, list) else v) for k, v in DEMO_MANIFEST.items()}
    apply_default_checkpoints(out)
    return out


# ---------------------------------------------------------------------------
# Seeding
# ---------------------------------------------------------------------------


def _build_demo_course_data() -> dict[str, Any]:
    """Produce the exact artifact payload the demo course writes.

    Kept as a pure function so tests can compare payloads across runs
    without invoking the full workflow machinery.
    """
    wf = CoursePrepWorkflow(
        instructor_id=DEMO_INSTRUCTOR_ID,
        course_id=DEMO_COURSE_ID,
        indexer=_NoopIndexer(len(DEMO_CORPUS)),
        retriever=_NoopRetriever(DEMO_CORPUS),
        llm=_NoopLLM(),
    )
    wf.load_manifest(_demo_manifest_with_defaults())
    wf.upload_corpus(DEMO_CORPUS)
    wf.preview_corpus("Newton's second law")
    wf.test_prompt(DEMO_SYSTEM_PROMPT, "State Newton's second law.")
    # Mark prompt as instructor-approved — the demo ships approved content.
    from .course_prep import validate_prompt_step

    wf.checklist = validate_prompt_step(
        wf.checklist,
        system_prompt=DEMO_SYSTEM_PROMPT,
        test_response=wf._last_prompt_test["response"] if wf._last_prompt_test else "",
        instructor_approved=True,
    )
    wf.assessments = list(DEMO_ASSESSMENTS)
    from .course_prep import validate_assessment_step, validate_rails_step

    wf.checklist = validate_assessment_step(
        wf.checklist, assessment_count=len(wf.assessments),
    )
    wf.rails = list(DEMO_RAILS)
    wf.checklist = validate_rails_step(wf.checklist, rail_count=len(wf.rails))

    return {
        "id": wf.course_id,
        "slug": DEMO_COURSE_SLUG,
        "title": DEMO_TITLE,
        "instructor_id": wf.instructor_id,
        "manifest": wf.manifest,
        "system_prompt": wf.system_prompt,
        "_last_prompt_test": wf._last_prompt_test,
        "assessments": wf.assessments,
        "rails": wf.rails,
        "corpus_doc_count": wf.corpus_doc_count,
        "last_preview": wf.last_preview,
        "steps": [asdict(s) for s in wf.checklist.steps],
    }


def _build_demo_classroom_data() -> dict[str, Any]:
    from .classroom_prep import (
        create_classroom_prep_checklist,
        validate_course_selected_step,
        validate_dry_run_step,
        validate_lms_step,
        validate_rag_policy_step,
    )

    checklist = create_classroom_prep_checklist(DEMO_INSTRUCTOR_ID, DEMO_CLASSROOM_ID)
    checklist = validate_course_selected_step(
        checklist,
        course_id=DEMO_COURSE_ID,
        course_version="1.0.0",
        publishable=True,
    )
    checklist = validate_rag_policy_step(
        checklist,
        policy_id=f"{DEMO_CLASSROOM_ID}-course_only",
        policy_name="Course only",
    )
    checklist = validate_lms_step(
        checklist, lms_connected=True, roster_count=len(DEMO_ROSTER),
    )
    checklist = validate_dry_run_step(checklist, dry_run_completed=True)

    return {
        "id": DEMO_CLASSROOM_ID,
        "slug": DEMO_CLASSROOM_SLUG,
        "title": DEMO_TITLE + " — Spring",
        "instructor_id": DEMO_INSTRUCTOR_ID,
        "course_id": DEMO_COURSE_ID,
        "course_slug": DEMO_COURSE_SLUG,
        "course_version": "1.0.0",
        "course_system_prompt": DEMO_SYSTEM_PROMPT,
        "rag_policy_mode": "course_only",
        "lms_roster": list(DEMO_ROSTER),
        "steps": [asdict(s) for s in checklist.steps],
    }


def seed_demo() -> None:
    """Seed (or re-seed) the demo course and classroom artifacts.

    Idempotent: running twice produces artifacts with identical payloads.
    The underlying ArtifactRegistry will version each write (v1, v2, ...)
    but ``load_course_data`` always returns the latest, which is
    byte-identical to the previous latest.

    Also populates the coordinator-side materials store so a student
    who joins the demo classroom downloads + indexes real corpus on
    join (without this step, ``axi classroom ask`` after joining a
    demo cohort returns "no matching passages" and the skeptic
    evaluation collapses).
    """
    from .operational_store import _reg

    reg = _reg()
    reg.register(kind="course", name=DEMO_COURSE_ID, data=_build_demo_course_data())
    reg.register(
        kind="classroom", name=DEMO_CLASSROOM_ID, data=_build_demo_classroom_data(),
    )
    _seed_demo_coordinator_materials()


def _seed_demo_coordinator_materials() -> None:
    """Write the demo corpus into the coordinator-side materials store.

    The HTTP coordinator serves materials from
    ``~/.axi/coordinator/classrooms/<classroom_id>/`` — independent of
    the operational store that ``seed_demo`` populates. Without seeding
    here, a real student joining the demo cohort gets an empty
    materials manifest and their local index stays blank.
    """
    from pathlib import Path

    from .classroom_materials import ClassroomMaterialsStore

    coord_classroom_dir = (
        Path.home() / ".axi" / "coordinator"
        / "classrooms" / DEMO_CLASSROOM_ID
    )
    materials = ClassroomMaterialsStore(coord_classroom_dir)
    # add_text is idempotent on (filename, content) — re-seeding the
    # demo doesn't multiply file copies on disk.
    for doc in DEMO_CORPUS:
        filename = f"{doc['id']}.md"
        materials.add_text(
            doc["text"],
            filename=filename,
            title=doc["title"],
        )


def reset_demo() -> None:
    """Tombstone existing demo artifacts then re-seed.

    Use when the user has mucked with the demo and wants a clean
    baseline. Also safe to call when no demo has been seeded yet —
    registry.delete on a missing id is a no-op.
    """
    from .operational_store import _reg

    reg = _reg()
    for kind, name in (("course", DEMO_COURSE_ID), ("classroom", DEMO_CLASSROOM_ID)):
        latest = reg.latest(kind=kind, name=name)
        if latest is not None:
            try:
                reg.delete(latest.id, reason="reset_demo")
            except Exception:
                pass
    seed_demo()


# ---------------------------------------------------------------------------
# Cloning
# ---------------------------------------------------------------------------


def clone_demo_course(new_course_id: str, instructor_id: str) -> str:
    """Copy the demo course into a fresh editable course under a new id.

    The demo course artifact is not modified. The new course's title
    drops the ``(Demo)`` suffix — it's the instructor's course now.
    Returns the new course id on success. Raises:

    - ``ValueError`` if ``new_course_id`` equals the demo course id
      (prevents accidental overwrite).
    - ``ValueError`` if a course with ``new_course_id`` already exists.
    """
    from .operational_store import _reg, load_course_data

    if new_course_id == DEMO_COURSE_ID:
        raise ValueError(
            f"cannot clone demo onto itself (new_course_id = {DEMO_COURSE_ID!r} "
            f"reserved for the demo course)"
        )
    if load_course_data(new_course_id) is not None:
        raise ValueError(f"course {new_course_id!r} already exists")

    demo_data = load_course_data(DEMO_COURSE_ID)
    if demo_data is None:
        # Seed the demo so cloning always succeeds — instructors who run
        # `prep from-demo` before `demo` shouldn't hit a dead end.
        seed_demo()
        demo_data = load_course_data(DEMO_COURSE_ID)
        assert demo_data is not None

    cloned: dict[str, Any] = dict(demo_data)
    cloned["id"] = new_course_id
    cloned["slug"] = new_course_id
    cloned["instructor_id"] = instructor_id
    # Strip the "(Demo)" marker from the title — it's no longer a demo.
    cloned["title"] = DEMO_TITLE.replace(" (Demo)", "")
    # Update the manifest's id to match the new course id so downstream
    # validators see a consistent record.
    manifest = dict(cloned.get("manifest") or {})
    manifest["id"] = new_course_id
    manifest["title"] = cloned["title"]
    cloned["manifest"] = manifest
    # Clear the demo's prompt-test transcript — the cloned course should
    # record its own test results.
    cloned["_last_prompt_test"] = None

    _reg().register(kind="course", name=new_course_id, data=cloned)
    return new_course_id


def clone_demo_classroom(
    *,
    new_classroom_id: str,
    new_course_id: str,
    instructor_id: str,
) -> str:
    """Copy the demo classroom into a fresh editable classroom.

    The classroom artifact is a thin instance record pointing at a
    course. Cloning rewrites the ``course_id`` to the caller's
    ``new_course_id`` so the classroom is bound to the instructor's
    cloned course, not the demo course.

    The demo classroom itself is never modified. Raises:

    - ``ValueError`` if ``new_classroom_id`` equals the demo classroom
      id.
    - ``ValueError`` if a classroom with ``new_classroom_id`` already
      exists.
    """
    from .operational_store import _reg, load_classroom_data

    if new_classroom_id == DEMO_CLASSROOM_ID:
        raise ValueError(
            f"cannot clone demo onto itself "
            f"(new_classroom_id = {DEMO_CLASSROOM_ID!r} reserved for the demo)"
        )
    if load_classroom_data(new_classroom_id) is not None:
        raise ValueError(
            f"classroom {new_classroom_id!r} already exists"
        )

    demo_data = load_classroom_data(DEMO_CLASSROOM_ID)
    if demo_data is None:
        seed_demo()
        demo_data = load_classroom_data(DEMO_CLASSROOM_ID)
        assert demo_data is not None

    cloned: dict[str, Any] = dict(demo_data)
    cloned["id"] = new_classroom_id
    cloned["slug"] = new_classroom_id
    cloned["instructor_id"] = instructor_id
    cloned["course_id"] = new_course_id
    cloned["course_slug"] = new_course_id
    cloned["title"] = DEMO_TITLE.replace(" (Demo)", "")
    # Reset lifecycle — cloned classroom starts unpublished regardless of
    # the demo's state. Instructor explicitly runs publish when ready.
    cloned["state"] = "unpublished"
    cloned.pop("published_at", None)
    cloned.pop("published_by", None)
    cloned.pop("archived_at", None)
    cloned.pop("archived_by", None)
    cloned.pop("archive_reason", None)

    _reg().register(kind="classroom", name=new_classroom_id, data=cloned)
    return new_classroom_id


def clone_demo(
    *,
    new_course_id: str,
    instructor_id: str,
    new_classroom_id: str | None = None,
) -> dict[str, str]:
    """Clone both the demo course AND a bound classroom.

    This is the CLI's default ``prep from-demo`` flow — instructors
    almost always want a runnable classroom, not a standalone course
    artifact.

    ``new_classroom_id`` defaults to ``new_course_id`` since course
    and classroom ids are in separate namespaces (different
    ArtifactRegistry ``kind``s) and collision-free. Callers who want
    cohort-specific classrooms (e.g. one classroom per semester)
    should supply an explicit id.

    Returns ``{"course_id": ..., "classroom_id": ...}``.
    """
    classroom_id = new_classroom_id or new_course_id

    course_id = clone_demo_course(
        new_course_id=new_course_id, instructor_id=instructor_id,
    )
    classroom_id = clone_demo_classroom(
        new_classroom_id=classroom_id,
        new_course_id=course_id,
        instructor_id=instructor_id,
    )
    return {"course_id": course_id, "classroom_id": classroom_id}


# ---------------------------------------------------------------------------
# Inline no-op backends — we don't need a real indexer/retriever/LLM to
# assemble the demo payload; the shipped fixtures are already the product.
# ---------------------------------------------------------------------------


class _NoopIndexer:
    def __init__(self, count: int) -> None:
        self._count = count

    def index(self, documents: list[dict]) -> int:
        return self._count


class _NoopRetriever:
    def __init__(self, docs: list[dict]) -> None:
        self._docs = docs

    def retrieve(self, query: str, k: int = 5) -> list[dict]:
        return list(self._docs[:k])


class _NoopLLM:
    def __call__(self, messages: list[dict], **kw: Any) -> str:
        return "Newton's second law states F = m*a."
