# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""AXI prep tools — chat-driven course prep (FW-1 P2).

Exposes the classroom prep workflow as chat tools so an instructor
can drive the flow from a conversation. Each tool wraps existing
workflow primitives; no new state machines. The TOOLS list is picked
up via the classroom extension manifest's ``[chat_tools]`` block,
then surfaced to the LLM by neut_agent's tool discovery.

Read-only tools auto-approve. Write tools are marked WRITE so the
existing HITL gate applies per RACI.

Phase-2 scope (see docs/working/design-instructor-prep-workflow.md):

- ``classroom_prep_status``            — READ   — show checklist for a classroom
- ``classroom_list_courses``           — READ   — enumerate courses in the store
- ``classroom_demo_seed``              — WRITE  — seed (or reset) the demo
- ``classroom_clone_from_demo``        — WRITE  — clone demo → editable course
- ``classroom_prep_extract_syllabus``  — READ   — propose manifest from syllabus text
                                          (pure analysis, no persistence)
- ``classroom_prep_tune_prompt``       — WRITE  — set + test a system prompt

P3 will add save-manifest, checkpoint configuration, rail customization.
"""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

# Absolute imports — this module is loaded by two discovery paths
# (neut_agent's tools_ext scan AND extensions/discovery manifest scan),
# and the latter loads under a synthetic package name that breaks
# relative imports.
from axiom.extensions.builtins.classroom.demo import (
    DEMO_CLASSROOM_ID,
    DEMO_COURSE_ID,
    DEMO_TITLE,
    clone_demo_course,
    reset_demo,
    seed_demo,
)
from axiom.extensions.builtins.classroom.operational_store import (
    _reg,
    load_classroom,
    load_course,
)
from axiom.infra.orchestrator.actions import ActionCategory

try:
    # ToolDef lives in neut_agent so we inherit the same shape other chat
    # tools use. Import is deferred-safe: if neut_agent isn't installed,
    # the TOOLS list stays empty and discovery silently skips us.
    from axiom.extensions.builtins.chat.tools import ToolDef
except Exception:  # pragma: no cover
    ToolDef = None  # type: ignore


# ---------------------------------------------------------------------------
# Tool definitions
# ---------------------------------------------------------------------------


TOOLS: list[Any] = []

if ToolDef is not None:
    TOOLS = [
        ToolDef(
            name="classroom_prep_status",
            description=(
                "Show the course + classroom prep checklist for a classroom. "
                "Use when the instructor asks about readiness, what's left, "
                "or the state of a specific classroom."
            ),
            category=ActionCategory.READ,
            parameters={
                "type": "object",
                "properties": {
                    "classroom_id": {
                        "type": "string",
                        "description": "Classroom ID to inspect",
                    },
                },
                "required": ["classroom_id"],
            },
        ),
        ToolDef(
            name="classroom_list_courses",
            description=(
                "List all courses in the local operational store. Use when the "
                "instructor asks what courses exist, which courses they've "
                "authored, or before cloning/editing a course."
            ),
            category=ActionCategory.READ,
            parameters={"type": "object", "properties": {}},
        ),
        ToolDef(
            name="classroom_demo_seed",
            description=(
                "Seed (or reset) the demo course + classroom so the instructor "
                "can try a fully-populated classroom immediately. Use when the "
                "instructor is new, wants to evaluate the platform, or asks "
                "for a working example."
            ),
            category=ActionCategory.WRITE,
            parameters={
                "type": "object",
                "properties": {
                    "reset": {
                        "type": "boolean",
                        "description": (
                            "If true, wipe existing demo artifacts before "
                            "reseeding (default: false)."
                        ),
                    },
                },
            },
        ),
        ToolDef(
            name="classroom_clone_from_demo",
            description=(
                "Clone the demo course into a new editable course under the "
                "instructor's identity. Use after the instructor has seen the "
                "demo and wants to customize it. Does not modify the demo itself."
            ),
            category=ActionCategory.WRITE,
            parameters={
                "type": "object",
                "properties": {
                    "new_course_id": {
                        "type": "string",
                        "description": (
                            "ID for the new course (must not collide with an "
                            "existing course)."
                        ),
                    },
                    "instructor_id": {
                        "type": "string",
                        "description": (
                            "Instructor principal, e.g. '@user:example-org' or "
                            "'user@example.org'."
                        ),
                    },
                },
                "required": ["new_course_id", "instructor_id"],
            },
        ),
        ToolDef(
            name="classroom_prep_extract_syllabus",
            description=(
                "Propose a course manifest from pasted syllabus text. Returns "
                "the proposed manifest as a dict; does NOT persist. Instructor "
                "reviews, then approves via a follow-up save call or CLI."
            ),
            category=ActionCategory.READ,
            parameters={
                "type": "object",
                "properties": {
                    "syllabus_text": {
                        "type": "string",
                        "description": (
                            "Plain text, markdown, or extracted PDF text of "
                            "the course syllabus."
                        ),
                    },
                },
                "required": ["syllabus_text"],
            },
        ),
        ToolDef(
            name="classroom_prep_dry_run_enhanced",
            description=(
                "Polished dry-run that retrieves from the course's actual "
                "corpus and returns a transcript showing grounded sample "
                "responses — what a student would experience. Use before "
                "publishing so the instructor can eyeball the end product."
            ),
            category=ActionCategory.READ,
            parameters={
                "type": "object",
                "properties": {
                    "classroom_id": {"type": "string"},
                    "queries": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "Optional — sample student queries to run. "
                            "Default: 3 canned queries covering Newton's "
                            "second law / momentum / work-energy."
                        ),
                    },
                },
                "required": ["classroom_id"],
            },
        ),
        ToolDef(
            name="classroom_wrap_template",
            description=(
                "Propose an updated CourseManifest for the next cohort "
                "based on this cohort's grade distributions. Advisory only "
                "— the instructor reviews and chooses what to adopt. Flags "
                "low-mean assessments, high-variance rubrics, and adds a "
                "retake checkpoint when failures are present."
            ),
            category=ActionCategory.READ,
            parameters={
                "type": "object",
                "properties": {
                    "classroom_id": {"type": "string"},
                },
                "required": ["classroom_id"],
            },
        ),
        ToolDef(
            name="classroom_wrap_grades",
            description=(
                "Compute per-student final grades from the grade ledger "
                "and optionally push to Canvas. v0 formula is equal-weight "
                "mean across all graded assessments. Defaults to compute-"
                "only — set push=true with a canvas_assignment_id to "
                "actually send grades to Canvas."
            ),
            category=ActionCategory.WRITE,
            parameters={
                "type": "object",
                "properties": {
                    "classroom_id": {"type": "string"},
                    "push": {
                        "type": "boolean",
                        "description": "Send to Canvas (default: false).",
                    },
                    "canvas_course_id": {"type": "string"},
                    "canvas_assignment_id": {
                        "type": "string",
                        "description": "Required when push=true.",
                    },
                },
                "required": ["classroom_id"],
            },
        ),
        ToolDef(
            name="classroom_wrap_harvest",
            description=(
                "Bundle a classroom's anonymized cohort data into a "
                ".axiompack zip for research export. Student principals "
                "are pseudonymized deterministically; names and emails "
                "are redacted. Use for paper writeups and longitudinal "
                "analysis across cohorts."
            ),
            category=ActionCategory.WRITE,
            parameters={
                "type": "object",
                "properties": {
                    "classroom_id": {"type": "string"},
                    "out_path": {
                        "type": "string",
                        "description": "Output path for the .axiompack file.",
                    },
                },
                "required": ["classroom_id", "out_path"],
            },
        ),
        ToolDef(
            name="classroom_wrap_analytics",
            description=(
                "Read-only cohort summary: roster size, course "
                "configuration (checkpoints / assessments / rails), "
                "grade-ledger distributions per assessment. Use during "
                "or after the course to review how the cohort went."
            ),
            category=ActionCategory.READ,
            parameters={
                "type": "object",
                "properties": {
                    "classroom_id": {"type": "string"},
                },
                "required": ["classroom_id"],
            },
        ),
        ToolDef(
            name="classroom_enroll",
            description=(
                "Enroll students into a published classroom. Pulls the "
                "roster from the configured LMS (or the fake mock server "
                "when ``fake=true``), generates per-student auth tokens, "
                "records nationality attestations signed by the "
                "instructor, and queues onboarding-rail checklists. "
                "Refuses to run on classrooms that aren't published."
            ),
            category=ActionCategory.WRITE,
            parameters={
                "type": "object",
                "properties": {
                    "classroom_id": {"type": "string"},
                    "instructor": {
                        "type": "string",
                        "description": "Instructor principal signing attestations.",
                    },
                    "fake": {
                        "type": "boolean",
                        "description": (
                            "If true, uses the populated CanvasMockServer "
                            "(5 synthetic students). Default false."
                        ),
                    },
                    "ttl_days": {
                        "type": "number",
                        "description": "Token lifetime in days (default 30).",
                    },
                    "canvas_course_id": {
                        "type": "string",
                        "description": (
                            "Override Canvas course id. Defaults to the "
                            "classroom's stored lms_course_id."
                        ),
                    },
                },
                "required": ["classroom_id", "instructor"],
            },
        ),
        ToolDef(
            name="classroom_archive",
            description=(
                "Archive a completed classroom (terminal lifecycle state; "
                "published → archived). Refuses if the classroom isn't "
                "currently published. Idempotent — re-archiving preserves "
                "the original archiver + timestamp."
            ),
            category=ActionCategory.WRITE,
            parameters={
                "type": "object",
                "properties": {
                    "classroom_id": {"type": "string"},
                    "archiver": {
                        "type": "string",
                        "description": "Instructor principal archiving the classroom.",
                    },
                    "reason": {
                        "type": "string",
                        "description": (
                            "Why the classroom is being archived "
                            "(e.g. 'End of Spring 2026 semester')."
                        ),
                    },
                },
                "required": ["classroom_id", "archiver", "reason"],
            },
        ),
        ToolDef(
            name="classroom_publish",
            description=(
                "Publish a prepared classroom (prep → published). Fails if "
                "either course OR classroom checklist isn't fully green; "
                "returns the list of blockers so the instructor knows what "
                "to fix. Once published, the classroom is bound to its "
                "course version and ready for student enrollment."
            ),
            category=ActionCategory.WRITE,
            parameters={
                "type": "object",
                "properties": {
                    "classroom_id": {"type": "string"},
                    "approver": {
                        "type": "string",
                        "description": (
                            "Instructor principal publishing the classroom "
                            "(e.g. @ben:ut)."
                        ),
                    },
                },
                "required": ["classroom_id", "approver"],
            },
        ),
        ToolDef(
            name="classroom_prep_lms_setup",
            description=(
                "Guided LMS walkthrough — list supported providers, probe "
                "Canvas connectivity, configure Canvas on a classroom, or "
                "mark a classroom as no-LMS (manual roster). Use when the "
                "instructor is setting up their institution's LMS."
            ),
            category=ActionCategory.WRITE,
            parameters={
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": [
                            "list-providers",
                            "canvas-probe",
                            "canvas-configure",
                            "none",
                        ],
                    },
                    "classroom_id": {
                        "type": "string",
                        "description": "Required for canvas-configure and none.",
                    },
                    "instance_url": {
                        "type": "string",
                        "description": "Canvas instance URL (canvas-probe, canvas-configure).",
                    },
                    "token": {
                        "type": "string",
                        "description": "Canvas API token (canvas-probe, canvas-configure).",
                    },
                    "canvas_course_id": {
                        "type": "string",
                        "description": "Canvas course id to sync roster from.",
                    },
                    "fake": {
                        "type": "boolean",
                        "description": (
                            "If true, routes through a populated "
                            "CanvasMockServer (demo + testing). Default: false."
                        ),
                    },
                },
                "required": ["action"],
            },
        ),
        ToolDef(
            name="classroom_prep_edit_rail",
            description=(
                "Edit a rail's YAML directly. Call without new_yaml to "
                "get the current YAML, then call again with the edited "
                "new_yaml to apply. Rejects id changes (remove + add "
                "instead) and invalid YAML. Useful when a rail needs "
                "custom branching or tweaks beyond what the bank "
                "provides."
            ),
            category=ActionCategory.WRITE,
            parameters={
                "type": "object",
                "properties": {
                    "course_id": {"type": "string"},
                    "rail_id": {"type": "string"},
                    "new_yaml": {
                        "type": "string",
                        "description": (
                            "Edited rail YAML. Omit to fetch current YAML "
                            "without applying changes."
                        ),
                    },
                },
                "required": ["course_id", "rail_id"],
            },
        ),
        ToolDef(
            name="classroom_prep_configure_rails",
            description=(
                "Manage onboarding rails — list installed question banks, "
                "add a rail to a course seeded from a bank, or preview a "
                "rail as a stub student to see what real students will "
                "experience. Use when the instructor is customizing the "
                "pre-course interview or data-consent flow."
            ),
            category=ActionCategory.WRITE,
            parameters={
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["list-banks", "add", "preview"],
                    },
                    "course_id": {
                        "type": "string",
                        "description": "Required for add/preview.",
                    },
                    "rail_id": {
                        "type": "string",
                        "description": "Required for add/preview.",
                    },
                    "bank_id": {
                        "type": "string",
                        "description": (
                            "Required for add. Bank id from list-banks."
                        ),
                    },
                    "question_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "Optional for add — subset of question ids from "
                            "the bank (default: all)."
                        ),
                    },
                    "required": {
                        "type": "boolean",
                        "description": "Optional for add (default: true).",
                    },
                },
                "required": ["action"],
            },
        ),
        ToolDef(
            name="classroom_prep_configure_checkpoints",
            description=(
                "List, add, remove, or opt out of course checkpoints "
                "(baseline, midpoint, final, or custom). Use when the "
                "instructor wants to review milestones, add an extra quiz, "
                "or skip the default assessment cadence. Timing accepts "
                "keywords (enrollment_complete, course_start, midway, "
                "course_end) or ISO-8601 dates (e.g. 2026-07-15)."
            ),
            category=ActionCategory.WRITE,
            parameters={
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["list", "add", "remove", "skip-defaults"],
                    },
                    "course_id": {"type": "string"},
                    "checkpoint_id": {
                        "type": "string",
                        "description": (
                            "Required for add/remove actions. For add, this "
                            "becomes the new checkpoint's id (also used as "
                            "default label)."
                        ),
                    },
                    "timing": {
                        "type": "string",
                        "description": (
                            "Required for add. Keyword or ISO-8601 date."
                        ),
                    },
                    "method": {
                        "type": "string",
                        "enum": ["quiz", "questionnaire", "portfolio", "observation", "none"],
                        "description": "Default: quiz.",
                    },
                    "label": {"type": "string"},
                    "questionnaire_id": {"type": "string"},
                    "required": {"type": "boolean"},
                },
                "required": ["action", "course_id"],
            },
        ),
        ToolDef(
            name="classroom_prep_tune_prompt",
            description=(
                "Set a system prompt on an existing course and test it with a "
                "sample query, returning the model's response so the instructor "
                "can iterate. Persists the prompt on the course artifact."
            ),
            category=ActionCategory.WRITE,
            parameters={
                "type": "object",
                "properties": {
                    "course_id": {"type": "string"},
                    "system_prompt": {
                        "type": "string",
                        "description": (
                            "The full system prompt to set for the course."
                        ),
                    },
                    "test_query": {
                        "type": "string",
                        "description": (
                            "Sample student question used to test the prompt "
                            "end-to-end."
                        ),
                    },
                },
                "required": ["course_id", "system_prompt", "test_query"],
            },
        ),
    ]


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------


def execute(name: str, params: dict[str, Any]) -> dict[str, Any]:
    """Dispatch a classroom-prep chat tool invocation."""
    if name == "classroom_prep_status":
        return _tool_prep_status(params)
    if name == "classroom_list_courses":
        return _tool_list_courses(params)
    if name == "classroom_demo_seed":
        return _tool_demo_seed(params)
    if name == "classroom_clone_from_demo":
        return _tool_clone_from_demo(params)
    if name == "classroom_prep_extract_syllabus":
        return _tool_extract_syllabus(params)
    if name == "classroom_prep_tune_prompt":
        return _tool_tune_prompt(params)
    if name == "classroom_prep_configure_checkpoints":
        return _tool_configure_checkpoints(params)
    if name == "classroom_prep_configure_rails":
        return _tool_configure_rails(params)
    if name == "classroom_prep_edit_rail":
        return _tool_edit_rail(params)
    if name == "classroom_prep_lms_setup":
        return _tool_lms_setup(params)
    if name == "classroom_prep_dry_run_enhanced":
        return _tool_dry_run_enhanced(params)
    if name == "classroom_publish":
        return _tool_publish(params)
    if name == "classroom_archive":
        return _tool_archive(params)
    if name == "classroom_enroll":
        return _tool_enroll(params)
    if name == "classroom_wrap_analytics":
        return _tool_wrap_analytics(params)
    if name == "classroom_wrap_harvest":
        return _tool_wrap_harvest(params)
    if name == "classroom_wrap_grades":
        return _tool_wrap_grades(params)
    if name == "classroom_wrap_template":
        return _tool_wrap_template(params)
    return {"error": f"Unknown classroom prep tool: {name}"}


# ---------------------------------------------------------------------------
# Tool implementations
# ---------------------------------------------------------------------------


def _tool_prep_status(params: dict) -> dict:
    classroom_id = params.get("classroom_id", "").strip()
    if not classroom_id:
        return {"error": "classroom_id is required"}

    loaded = load_classroom(classroom_id)
    if loaded is None:
        return {"error": f"classroom {classroom_id!r} not found"}
    classroom_wf, classroom_data = loaded

    course_id = classroom_data.get("course_id", "")
    course_loaded = load_course(course_id) if course_id else None

    course_ready = False
    course_checklist: list[dict] = []
    if course_loaded is not None:
        course_wf, _course_data = course_loaded
        course_ready, _ = course_wf.is_ready_to_publish()
        course_checklist = [asdict(s) for s in course_wf.checklist.steps]

    classroom_ready, _ = classroom_wf.is_ready_for_enrollment()
    classroom_checklist = [asdict(s) for s in classroom_wf.checklist.steps]

    return {
        "classroom_id": classroom_id,
        "course_id": course_id,
        "course_ready": course_ready,
        "classroom_ready": classroom_ready,
        "title": classroom_data.get("title", ""),
        "checklist": course_checklist + classroom_checklist,
    }


def _tool_list_courses(params: dict) -> dict:
    courses = []
    for artifact in _reg().list(kind="course"):
        data = artifact.data
        if not data:
            continue
        courses.append(
            {
                "id": data.get("id", ""),
                "title": data.get("title", ""),
                "instructor_id": data.get("instructor_id", ""),
                "corpus_doc_count": data.get("corpus_doc_count", 0),
            }
        )
    # Dedup by id — the registry returns full version chain; we only want
    # the latest of each course. Using a dict by id keeps first-seen, but
    # list() returns in creation order, so iterate reversed and keep the
    # latest per id.
    seen: dict[str, dict] = {}
    for c in courses:
        seen[c["id"]] = c
    unique = list(seen.values())
    return {"courses": unique, "count": len(unique)}


def _tool_demo_seed(params: dict) -> dict:
    reset = bool(params.get("reset", False))
    if reset:
        reset_demo()
        action = "reset"
    else:
        seed_demo()
        action = "seeded"
    return {
        "action": action,
        "course_id": DEMO_COURSE_ID,
        "classroom_id": DEMO_CLASSROOM_ID,
        "title": DEMO_TITLE,
    }


def _tool_clone_from_demo(params: dict) -> dict:
    new_course_id = params.get("new_course_id", "").strip()
    instructor_id = params.get("instructor_id", "").strip()
    if not new_course_id or not instructor_id:
        return {"error": "new_course_id and instructor_id are both required"}
    try:
        new_id = clone_demo_course(
            new_course_id=new_course_id, instructor_id=instructor_id,
        )
    except ValueError as e:
        return {"error": str(e)}
    return {"cloned_course_id": new_id, "instructor_id": instructor_id}


def _tool_extract_syllabus(params: dict) -> dict:
    syllabus_text = params.get("syllabus_text", "")
    if not syllabus_text.strip():
        return {"error": "syllabus_text is required (paste the syllabus as text)"}
    # Lazy import — syllabus_extraction pulls in the structured_output
    # machinery which does real LLM work; we don't want to load it for
    # tools that never call it.
    from axiom.extensions.builtins.classroom import syllabus_extraction

    try:
        manifest = syllabus_extraction.extract_syllabus_manifest(syllabus_text)
    except Exception as e:
        return {"error": f"syllabus extraction failed: {e}"}
    return {
        "proposed_manifest": manifest.to_dict(),
        "note": (
            "Proposed manifest not yet persisted. Review the fields; call "
            "the save tool (P3) or run `axi classroom prep init --manifest` "
            "to apply."
        ),
    }


def _tool_dry_run_enhanced(params: dict) -> dict:
    from axiom.extensions.builtins.classroom.publish import enhanced_dry_run

    classroom_id = params.get("classroom_id", "").strip()
    if not classroom_id:
        return {"error": "classroom_id is required"}
    queries = params.get("queries") or None
    return enhanced_dry_run(classroom_id=classroom_id, queries=queries)


def _tool_publish(params: dict) -> dict:
    from axiom.extensions.builtins.classroom.publish import publish_classroom

    classroom_id = params.get("classroom_id", "").strip()
    approver = params.get("approver", "").strip()
    if not classroom_id or not approver:
        return {
            "published": False,
            "error": "classroom_id and approver are both required",
        }
    return publish_classroom(classroom_id=classroom_id, approver=approver)


def _tool_wrap_analytics(params: dict) -> dict:
    from axiom.extensions.builtins.classroom.conclusion import summarize_classroom

    classroom_id = params.get("classroom_id", "").strip()
    if not classroom_id:
        return {"error": "classroom_id is required"}
    return summarize_classroom(classroom_id)


def _tool_wrap_harvest(params: dict) -> dict:
    from axiom.extensions.builtins.classroom.conclusion import harvest_classroom

    classroom_id = params.get("classroom_id", "").strip()
    out_path = params.get("out_path", "").strip()
    if not classroom_id or not out_path:
        return {
            "harvested": False,
            "error": "classroom_id and out_path are both required",
        }
    return harvest_classroom(classroom_id=classroom_id, out_path=out_path)


def _tool_wrap_template(params: dict) -> dict:
    from axiom.extensions.builtins.classroom.conclusion import (
        propose_template_update,
    )

    classroom_id = params.get("classroom_id", "").strip()
    if not classroom_id:
        return {"error": "classroom_id is required"}
    return propose_template_update(classroom_id=classroom_id)


def _tool_wrap_grades(params: dict) -> dict:
    from axiom.extensions.builtins.classroom.conclusion import finalize_grades

    classroom_id = params.get("classroom_id", "").strip()
    if not classroom_id:
        return {"error": "classroom_id is required"}
    return finalize_grades(
        classroom_id=classroom_id,
        push=bool(params.get("push", False)),
        canvas_course_id=(params.get("canvas_course_id") or None),
        canvas_assignment_id=(params.get("canvas_assignment_id") or None),
    )


def _tool_archive(params: dict) -> dict:
    from axiom.extensions.builtins.classroom.archive import archive_classroom

    classroom_id = params.get("classroom_id", "").strip()
    archiver = params.get("archiver", "").strip()
    reason = params.get("reason", "").strip()
    if not classroom_id or not archiver or not reason:
        return {
            "archived": False,
            "error": "classroom_id, archiver, and reason are all required",
        }
    return archive_classroom(
        classroom_id=classroom_id, archiver=archiver, reason=reason,
    )


def _tool_enroll(params: dict) -> dict:
    from axiom.extensions.builtins.classroom.enroll_runner import run_enrollment

    classroom_id = params.get("classroom_id", "").strip()
    instructor = params.get("instructor", "").strip()
    if not classroom_id or not instructor:
        return {
            "enrolled": False,
            "error": "classroom_id and instructor are both required",
        }
    ttl_days = int(params.get("ttl_days") or 30)
    return run_enrollment(
        classroom_id=classroom_id,
        instructor=instructor,
        fake=bool(params.get("fake", False)),
        ttl_days=ttl_days,
        canvas_course_id=(params.get("canvas_course_id") or None),
    )


def _tool_lms_setup(params: dict) -> dict:
    """Implements list-providers / canvas-probe / canvas-configure / none."""
    from axiom.extensions.builtins.classroom.lms_setup import (
        build_fake_canvas_for_cli,
        canvas_configure,
        canvas_probe,
        list_providers,
        mark_no_lms,
    )

    action = params.get("action", "").strip()
    if not action:
        return {"error": "action is required"}

    if action == "list-providers":
        providers = list_providers()
        return {"providers": providers, "count": len(providers)}

    fake = bool(params.get("fake", False))

    if action == "canvas-probe":
        instance_url = params.get("instance_url", "").strip()
        token = params.get("token", "").strip()
        if not instance_url or not token:
            return {
                "connected": False,
                "error": "canvas-probe requires instance_url and token",
            }
        mock = build_fake_canvas_for_cli() if fake else None
        return canvas_probe(instance_url=instance_url, token=token, mock_server=mock)

    if action == "canvas-configure":
        classroom_id = params.get("classroom_id", "").strip()
        instance_url = params.get("instance_url", "").strip()
        token = params.get("token", "").strip()
        canvas_course_id = params.get("canvas_course_id", "").strip()
        if not classroom_id or not instance_url or not token or not canvas_course_id:
            return {
                "configured": False,
                "error": (
                    "canvas-configure requires classroom_id, instance_url, "
                    "token, canvas_course_id"
                ),
            }
        mock = build_fake_canvas_for_cli() if fake else None
        return canvas_configure(
            classroom_id=classroom_id,
            instance_url=instance_url,
            token=token,
            canvas_course_id=canvas_course_id,
            mock_server=mock,
        )

    if action == "none":
        classroom_id = params.get("classroom_id", "").strip()
        if not classroom_id:
            return {"no_lms": False, "error": "none requires classroom_id"}
        return mark_no_lms(classroom_id=classroom_id)

    return {
        "error": (
            f"unknown action {action!r}: expected list-providers|"
            f"canvas-probe|canvas-configure|none"
        )
    }


def _tool_edit_rail(params: dict) -> dict:
    from axiom.extensions.builtins.classroom.rail_edit import (
        apply_rail_edit,
        load_rail_for_edit,
    )

    course_id = params.get("course_id", "").strip()
    rail_id = params.get("rail_id", "").strip()
    if not course_id or not rail_id:
        return {"error": "course_id and rail_id are both required"}

    new_yaml = params.get("new_yaml", "")
    if not new_yaml:
        # No edit submitted — return the current YAML for the caller to edit.
        try:
            current = load_rail_for_edit(
                course_id=course_id, rail_id=rail_id,
            )
        except ValueError as e:
            return {"error": str(e)}
        return {"current_yaml": current}

    return apply_rail_edit(
        course_id=course_id, rail_id=rail_id, new_yaml=new_yaml,
    )


def _tool_configure_rails(params: dict) -> dict:
    """Implements list-banks/add/preview for onboarding rails."""
    from axiom.extensions.builtins.classroom.question_banks import (
        add_rail_from_bank,
        list_banks,
        preview_rail,
    )

    action = params.get("action", "").strip()
    if not action:
        return {"error": "action is required"}

    if action == "list-banks":
        banks = [b.to_dict() for b in list_banks()]
        return {"banks": banks, "count": len(banks)}

    course_id = params.get("course_id", "").strip()
    if not course_id:
        return {"error": f"{action} requires course_id"}

    loaded = load_course(course_id)
    if loaded is None:
        return {"error": f"course {course_id!r} not found"}
    _, data = loaded
    manifest = dict(data.get("manifest") or {})
    # Rails may live on the manifest or at the course top level.
    if "rails" not in manifest:
        manifest["rails"] = list(data.get("rails") or [])

    if action == "add":
        rail_id = params.get("rail_id", "").strip()
        bank_id = params.get("bank_id", "").strip()
        if not rail_id or not bank_id:
            return {"error": "add requires rail_id and bank_id"}
        question_ids = params.get("question_ids") or None
        required = params.get("required")
        required_flag = True if required is None else bool(required)
        try:
            rail = add_rail_from_bank(
                manifest,
                rail_id=rail_id,
                bank_id=bank_id,
                question_ids=question_ids,
                required=required_flag,
            )
        except ValueError as e:
            return {"error": str(e)}
        # Persist both locations (manifest + top-level rails mirror) so the
        # legacy workflow sees the change.
        updated = dict(data)
        updated["manifest"] = manifest
        updated["rails"] = list(manifest.get("rails") or [])
        from axiom.extensions.builtins.classroom.operational_store import _reg

        _reg().register(kind="course", name=course_id, data=updated)
        return {
            "course_id": course_id,
            "added": rail,
            "count": len(manifest.get("rails") or []),
        }

    if action == "preview":
        rail_id = params.get("rail_id", "").strip()
        if not rail_id:
            return {"error": "preview requires rail_id"}
        try:
            return preview_rail(manifest, rail_id=rail_id)
        except ValueError as e:
            return {"error": str(e)}

    return {
        "error": f"unknown action {action!r}: expected list-banks|add|preview"
    }


def _tool_configure_checkpoints(params: dict) -> dict:
    """Implements list/add/remove/skip-defaults for checkpoints."""
    from axiom.extensions.builtins.classroom.checkpoints import (
        add_checkpoint,
        list_checkpoints,
        remove_checkpoint,
        skip_defaults,
    )

    action = params.get("action", "").strip()
    course_id = params.get("course_id", "").strip()
    if not action or not course_id:
        return {"error": "action and course_id are both required"}

    loaded = load_course(course_id)
    if loaded is None:
        return {"error": f"course {course_id!r} not found"}
    _, data = loaded
    manifest = dict(data.get("manifest") or {})

    if action == "list":
        items = list_checkpoints(manifest)
        return {
            "course_id": course_id,
            "checkpoints": items,
            "count": len(items),
        }

    if action == "add":
        ck_id = params.get("checkpoint_id", "").strip()
        timing = params.get("timing", "").strip()
        if not ck_id or not timing:
            return {
                "error": (
                    "add requires checkpoint_id and timing "
                    "(keyword or ISO-8601 date)"
                )
            }
        try:
            added = add_checkpoint(
                manifest,
                {
                    "id": ck_id,
                    "label": params.get("label") or ck_id,
                    "timing": timing,
                    "method": params.get("method", "quiz"),
                    "questionnaire_id": params.get("questionnaire_id", ""),
                    "required": bool(params.get("required", False)),
                },
            )
        except ValueError as e:
            return {"error": str(e)}
        _persist_manifest(course_id, data, manifest)
        return {
            "course_id": course_id,
            "added": added,
            "checkpoints": manifest.get("checkpoints", []),
            "count": len(manifest.get("checkpoints", [])),
        }

    if action == "remove":
        ck_id = params.get("checkpoint_id", "").strip()
        if not ck_id:
            return {"error": "remove requires checkpoint_id"}
        removed = remove_checkpoint(manifest, ck_id)
        if not removed:
            return {"error": f"checkpoint {ck_id!r} not found"}
        _persist_manifest(course_id, data, manifest)
        return {
            "course_id": course_id,
            "removed_id": ck_id,
            "checkpoints": manifest.get("checkpoints", []),
            "count": len(manifest.get("checkpoints", [])),
        }

    if action == "skip-defaults":
        skip_defaults(manifest)
        _persist_manifest(course_id, data, manifest)
        return {"course_id": course_id, "checkpoints": [], "count": 0}

    return {
        "error": f"unknown action {action!r}: expected list|add|remove|skip-defaults"
    }


def _persist_manifest(course_id: str, data: dict, manifest: dict) -> None:
    from axiom.extensions.builtins.classroom.operational_store import _reg

    updated = dict(data)
    updated["manifest"] = manifest
    _reg().register(kind="course", name=course_id, data=updated)


def _tool_tune_prompt(params: dict) -> dict:
    course_id = params.get("course_id", "").strip()
    system_prompt = params.get("system_prompt", "").strip()
    test_query = params.get("test_query", "").strip()
    if not course_id or not system_prompt or not test_query:
        return {
            "error": (
                "course_id, system_prompt, and test_query are all required"
            )
        }

    loaded = load_course(course_id)
    if loaded is None:
        return {"error": f"course {course_id!r} not found"}
    wf, data = loaded

    try:
        response = wf.test_prompt(system_prompt, test_query)
    except Exception as e:
        return {"error": f"prompt test failed: {e}"}

    # "tune" semantically means "set + test + done" — mark the checklist
    # step completed so the course becomes publishable. Instructors who
    # want a review gate between test and approval should use the
    # step-by-step ``prep prompt --set --test --approve`` CLI instead.
    from axiom.extensions.builtins.classroom.course_prep import (
        validate_prompt_step,
    )

    wf.checklist = validate_prompt_step(
        wf.checklist,
        system_prompt=system_prompt,
        test_response=response,
        instructor_approved=True,
    )

    # Persist: overwrite the course artifact with the new prompt state.
    from axiom.extensions.builtins.classroom.operational_store import save_course

    slug = data.get("slug", course_id)
    title = data.get("title", "")
    save_course(wf, slug=slug, title=title)

    return {
        "course_id": course_id,
        "system_prompt": system_prompt,
        "test_query": test_query,
        "test_response": response,
        "approved": True,
    }
