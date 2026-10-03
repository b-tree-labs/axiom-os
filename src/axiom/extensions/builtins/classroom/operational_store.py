# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Operational store for course + classroom state (#74).

Replaces the earlier JSON-file persistence at runtime/courses/<id>/course.json
and runtime/classrooms/<id>/classroom.json with an ArtifactRegistry-backed
store. Artifacts carry:

- Content-addressing (SHA-256 of canonical payload)
- Version chains (new state with same name → v2, v3, ...)
- Optional signatures (when a signer is provided)
- Tombstone deletion
- Persistent SQLite backend

Two registries:
- `runtime/operational.db` for course + classroom state
- Per-classroom `runtime/classrooms/<id>/artifacts.db` for memory
  fragments (owned by CompositionService)

They are intentionally separate stores — operational workflow state is
a different concern from memory fragments, and a course outlives any
single classroom instance.

Per ADR-027 federation: this operational store is the local node's
authoritative source of truth for the courses and classrooms it hosts.
Federation peers advertise + replicate via the cohort registry
(cohort_registry.py) separately.
"""

from __future__ import annotations

import os
from dataclasses import asdict
from pathlib import Path
from typing import Any

from axiom.artifacts.registry import ArtifactRegistry, SQLiteBackend

from .classroom_prep import ClassroomPrepChecklist, ClassroomPrepStep
from .classroom_prep_workflow import ClassroomPrepWorkflow
from .course_prep import CoursePrepChecklist, PrepStep
from .course_prep_workflow import CoursePrepWorkflow

# ---------------------------------------------------------------------------
# Runtime paths
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


def _operational_db_path() -> Path:
    root = _runtime_root()
    root.mkdir(parents=True, exist_ok=True)
    return root / "operational.db"


# ---------------------------------------------------------------------------
# Registry singleton (per-process)
# ---------------------------------------------------------------------------


_registry: ArtifactRegistry | None = None


def _reg() -> ArtifactRegistry:
    """Lazily construct a per-process ArtifactRegistry for operational state.

    A test monkeypatching AXIOM_RUNTIME_ROOT between runs will invalidate
    the cached registry. We check the current runtime_root each call and
    rebuild if it changed.
    """
    global _registry
    current_path = _operational_db_path()
    if _registry is None or getattr(_registry._backend, "_path", None) != current_path:
        _registry = ArtifactRegistry(backend=SQLiteBackend(current_path))
    return _registry


# ---------------------------------------------------------------------------
# Course state
# ---------------------------------------------------------------------------


def save_course(
    wf: CoursePrepWorkflow,
    slug: str,
    title: str | None,
) -> str:
    """Persist course state as an Artifact. Returns the artifact id."""
    data: dict[str, Any] = {
        "id": wf.course_id,
        "slug": slug,
        "title": title,
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
    return _reg().register(
        kind="course",
        name=wf.course_id,
        data=data,
    )


def load_course(course_id: str) -> tuple[CoursePrepWorkflow, dict] | None:
    """Load the most-recent non-deleted course artifact, rehydrate workflow."""
    from .cli import _stub_llm, _StubIndexer, _StubRetriever

    artifact = _reg().latest(kind="course", name=course_id)
    if artifact is None:
        return None
    data = artifact.data

    wf = CoursePrepWorkflow(
        instructor_id=data.get("instructor_id", ""),
        course_id=data.get("id", course_id),
        indexer=_StubIndexer(),
        retriever=_StubRetriever([]),
        llm=_stub_llm,
    )
    wf.manifest = data.get("manifest")
    wf.system_prompt = data.get("system_prompt")
    wf._last_prompt_test = data.get("_last_prompt_test")
    wf.assessments = data.get("assessments", [])
    wf.rails = data.get("rails", [])
    wf.corpus_doc_count = data.get("corpus_doc_count", 0)
    wf.last_preview = data.get("last_preview", [])

    restored = [PrepStep(**s) for s in data.get("steps", [])]
    if restored:
        wf.checklist = CoursePrepChecklist(
            instructor_id=wf.instructor_id,
            course_id=wf.course_id,
            steps=restored,
        )
    return wf, data


# ---------------------------------------------------------------------------
# Classroom state
# ---------------------------------------------------------------------------


def save_classroom(
    wf: ClassroomPrepWorkflow,
    slug: str,
    title: str | None,
    course_id: str,
    course_slug: str,
) -> str:
    data: dict[str, Any] = {
        "id": wf.classroom_id,
        "slug": slug,
        "title": title,
        "instructor_id": wf.instructor_id,
        "course_id": course_id,
        "course_slug": course_slug,
        "course_version": wf.course_version,
        "course_system_prompt": wf.course_system_prompt,
        "rag_policy_mode": wf.rag_policy_mode,
        "lms_roster": wf.lms_roster,
        "steps": [asdict(s) for s in wf.checklist.steps],
    }
    return _reg().register(
        kind="classroom",
        name=wf.classroom_id,
        data=data,
    )


def load_classroom_data(classroom_id: str) -> dict | None:
    """Read-only accessor to classroom state dict (no workflow rehydration).

    Useful for surfaces like MCP servers that only need the data shape,
    not a fully constructed workflow. Missing fields don't error.
    """
    artifact = _reg().latest(kind="classroom", name=classroom_id)
    if artifact is None:
        return None
    return artifact.data


def load_course_data(course_id: str) -> dict | None:
    """Read-only accessor to course state dict (no workflow rehydration)."""
    artifact = _reg().latest(kind="course", name=course_id)
    if artifact is None:
        return None
    return artifact.data


def load_classroom(classroom_id: str) -> tuple[ClassroomPrepWorkflow, dict] | None:
    from .cli import _stub_llm, _StubLMSOffline, _StubRetriever

    artifact = _reg().latest(kind="classroom", name=classroom_id)
    if artifact is None:
        return None
    data = artifact.data

    wf = ClassroomPrepWorkflow(
        instructor_id=data.get("instructor_id", ""),
        classroom_id=data.get("id", classroom_id),
        retriever=_StubRetriever([]),
        llm=_stub_llm,
        lms=_StubLMSOffline(0),
    )
    wf.course_id = data.get("course_id")
    wf.course_version = data.get("course_version")
    wf.course_system_prompt = data.get("course_system_prompt")
    wf.rag_policy_mode = data.get("rag_policy_mode")
    wf.lms_roster = data.get("lms_roster", [])

    restored = [ClassroomPrepStep(**s) for s in data.get("steps", [])]
    if restored:
        wf.checklist = ClassroomPrepChecklist(
            instructor_id=wf.instructor_id,
            classroom_id=wf.classroom_id,
            course_id=wf.course_id,
            course_version=wf.course_version,
            steps=restored,
        )
    return wf, data


# ---------------------------------------------------------------------------
# Resource-fragment tagging (bring course/classroom state into memory layer)
# ---------------------------------------------------------------------------


def record_course_resource_fragment(
    composition,
    course_id: str,
    course_slug: str,
    instructor_id: str,
):
    """Write a MemoryFragment(resource) referencing the course artifact.

    The operational_store holds the course's workflow state as an
    Artifact. This companion fragment brings the course into the
    unified memory layer with ownership + signing + audit — enabling
    RPE, trust, access to reason about courses as first-class memory.
    """
    from axiom.memory.ownership import new_ownership

    return composition.write(
        content={
            "ref": f"axiom-local:course/{course_id}",
            "course_id": course_id,
            "course_slug": course_slug,
            "fact_kind": "course_resource",
        },
        cognitive_type="resource",
        principal_id=instructor_id,
        agents=set(),
        resources={f"course:{course_id}"},
        ownership=new_ownership(master=instructor_id),
    )


def record_classroom_resource_fragment(
    composition,
    classroom_id: str,
    classroom_slug: str,
    course_id: str,
    instructor_id: str,
):
    """Write a MemoryFragment(resource) for the classroom instance."""
    from axiom.memory.ownership import new_ownership

    return composition.write(
        content={
            "ref": f"axiom-local:classroom/{classroom_id}",
            "classroom_id": classroom_id,
            "classroom_slug": classroom_slug,
            "course_id": course_id,
            "fact_kind": "classroom_resource",
        },
        cognitive_type="resource",
        principal_id=instructor_id,
        agents=set(),
        resources={f"classroom:{classroom_id}", f"course:{course_id}"},
        ownership=new_ownership(master=instructor_id),
    )
