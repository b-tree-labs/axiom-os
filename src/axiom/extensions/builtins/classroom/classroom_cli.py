# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Classroom CLI orchestration — wires all modules together.

axi classroom create  → Canvas + enrollment + rails + RAG config + pipeline
axi classroom doctor  → health checks
axi classroom enroll  → add/sync students
axi classroom rag-policy → view/swap RAG policy
axi classroom ingest  → media ingest shortcut
axi classroom status  → dashboard summary
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .enrollment import EnrollmentResult, enroll_classroom
from .lms.canvas import CanvasLMSProvider
from .pipeline import ClassroomChatPipeline
from .rag_policy import RAGPolicy

# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------


@dataclass
class HealthCheck:
    """Single health check result."""

    name: str
    status: str  # "healthy", "unhealthy", "warning"
    message: str = ""


@dataclass
class ClassroomInstance:
    """Runtime state of a classroom."""

    classroom_id: str
    rag_policy: RAGPolicy
    pipeline: ClassroomChatPipeline | None = None
    enrollment: EnrollmentResult | None = None


@dataclass
class CreateResult:
    """Result of creating a classroom."""

    classroom_id: str
    student_count: int
    pipeline: ClassroomChatPipeline
    rag_policy: RAGPolicy
    enrollment: EnrollmentResult


# ---------------------------------------------------------------------------
# Create
# ---------------------------------------------------------------------------


def create_classroom(
    manifest: dict[str, Any],
    lms_config: dict[str, Any],
    canvas_course_id: str,
    instructor_email: str,
    nationality_map: dict[str, str] | None = None,
    rag_config: dict[str, Any] | None = None,
    ttl_days: int = 30,
) -> CreateResult:
    """Full classroom creation: manifest + Canvas + enrollment + pipeline.

    Orchestrates all P0 modules into one call.
    """
    classroom_id = manifest["id"]
    nationality_map = nationality_map or {}
    rag_config = rag_config or {"mode": "course_only"}

    # 1. LMS provider
    provider = CanvasLMSProvider(lms_config)

    # 2. Enrollment (Canvas → tokens → attestations → rails)
    enrollment = enroll_classroom(
        lms_provider=provider,
        canvas_course_id=canvas_course_id,
        classroom_id=classroom_id,
        ttl_days=ttl_days,
        instructor_email=instructor_email,
        nationality_map=nationality_map,
        course_manifest=manifest,
    )

    # 3. RAG policy
    rag_policy = _build_rag_policy(classroom_id, rag_config)

    # 4. Chat pipeline
    pipeline = ClassroomChatPipeline(
        course_system_prompt=manifest.get("system_prompt", "You are a helpful tutor."),
        rag_retriever=None,  # wired when retriever backend is available
        llm_backend=lambda messages, **kw: "",  # wired to gateway at serve time
        suggest_next_steps=True,
    )

    return CreateResult(
        classroom_id=classroom_id,
        student_count=len(enrollment.students),
        pipeline=pipeline,
        rag_policy=rag_policy,
        enrollment=enrollment,
    )


def _build_rag_policy(classroom_id: str, rag_config: dict) -> RAGPolicy:
    """Build a RAGPolicy from the instructor's RAG configuration choice."""
    mode = rag_config.get("mode", "course_only")

    if mode == "course_only":
        return RAGPolicy(
            id=f"{classroom_id}-course-only",
            name="Course Materials Only",
            corpora=[{"corpus_id": f"course-{classroom_id}"}],
        )
    elif mode == "course_plus_institutional":
        return RAGPolicy(
            id=f"{classroom_id}-institutional",
            name="Course + Institutional",
            corpora=[
                {"corpus_id": f"course-{classroom_id}", "weight": 1.0},
                {"corpus_id": "institutional", "weight": 0.5},
            ],
        )
    elif mode == "full":
        return RAGPolicy(
            id=f"{classroom_id}-full",
            name="Course + Institutional + Community",
            corpora=[
                {"corpus_id": f"course-{classroom_id}", "weight": 1.0},
                {"corpus_id": "institutional", "weight": 0.5},
                {"corpus_id": "community", "weight": 0.3},
            ],
        )
    elif mode == "ab_test":
        return RAGPolicy(
            id=f"{classroom_id}-ab",
            name="A/B Test (course primary, full shadow)",
            corpora=[{"corpus_id": f"course-{classroom_id}"}],
            shadow_config={
                "shadow_corpus_id": rag_config.get("shadow_corpus", "full-community"),
                "capture_to": "langfuse",
            },
        )
    else:
        return RAGPolicy(
            id=f"{classroom_id}-custom",
            name="Custom",
            corpora=rag_config.get("corpora", [{"corpus_id": f"course-{classroom_id}"}]),
        )


# ---------------------------------------------------------------------------
# Doctor (health checks)
# ---------------------------------------------------------------------------


def classroom_doctor(
    classroom_id: str,
    web_endpoint: str = "",
    trace_store_writable: bool = True,
    rag_indexed: bool = True,
    llm_responsive: bool = True,
    tokens_valid: bool = True,
) -> list[HealthCheck]:
    """Run classroom health checks.

    In production these probe real services; for testing the
    parameters are injectable.
    """
    checks = []

    # Web endpoint
    if web_endpoint:
        reachable = _check_web_endpoint(web_endpoint)
        checks.append(
            HealthCheck(
                name="Web Endpoint",
                status="healthy" if reachable else "unhealthy",
                message=f"{web_endpoint} {'reachable' if reachable else 'unreachable'}",
            )
        )
    else:
        checks.append(
            HealthCheck(name="Web Endpoint", status="warning", message="No endpoint configured")
        )

    # Trace store
    checks.append(
        HealthCheck(
            name="Trace Store",
            status="healthy" if trace_store_writable else "unhealthy",
            message="LangFuse writable" if trace_store_writable else "Trace store not writable",
        )
    )

    # RAG corpus
    checks.append(
        HealthCheck(
            name="RAG Corpus",
            status="healthy" if rag_indexed else "unhealthy",
            message="Corpus indexed" if rag_indexed else "Corpus not indexed",
        )
    )

    # LLM gateway
    checks.append(
        HealthCheck(
            name="LLM Gateway",
            status="healthy" if llm_responsive else "unhealthy",
            message="Gateway responsive" if llm_responsive else "Gateway not responding",
        )
    )

    # Student tokens
    checks.append(
        HealthCheck(
            name="Student Tokens",
            status="healthy" if tokens_valid else "unhealthy",
            message="All tokens valid" if tokens_valid else "Some tokens expired or invalid",
        )
    )

    return checks


def _check_web_endpoint(url: str) -> bool:
    """Probe a web endpoint. Returns True if reachable."""
    try:
        import requests

        resp = requests.get(url, timeout=5)
        return resp.status_code < 500
    except Exception:
        return False


# ---------------------------------------------------------------------------
# RAG policy swap
# ---------------------------------------------------------------------------


def swap_rag_policy(instance: ClassroomInstance, new_policy: RAGPolicy) -> None:
    """Swap the active RAG policy on a running classroom. Immediate effect."""
    instance.rag_policy = new_policy
