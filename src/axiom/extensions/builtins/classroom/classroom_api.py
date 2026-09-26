# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""FastAPI app factory for the classroom coordinator.

Replaces the hand-rolled ``coordinator_server.py`` stdlib
``BaseHTTPRequestHandler`` prototype with a proper FastAPI app, built
on the ``http`` extension's ``create_app`` + ``ThreadedServer``
primitives. Wire compatibility is exact — every URL + payload is
unchanged so existing student CLIs + tests keep working on the new
server.

Entry point :func:`create_classroom_app` takes the same stores that
the legacy handler did and mounts routers that cover:

    POST /classroom/join                        — join ceremony
    POST /classroom/interaction                 — ask log
    POST /classroom/threads/open                — thread open
    POST /classroom/threads/<id>/reply          — thread reply
    POST /classroom/quizzes/<id>/submit         — quiz submit
    GET  /classroom/policy                      — mode policy
    GET  /classroom/materials/manifest          — signed manifest
    GET  /classroom/materials/<file_id>         — raw file
    GET  /classroom/briefs/<student_id>         — approved brief
    GET  /classroom/threads                     — threads list
    GET  /classroom/threads/<id>                — thread fetch
    GET  /classroom/quizzes/pending             — pending quizzes
    GET  /classroom/quizzes/<id>                — quiz detail
    GET  /                                      — landing HTML
    GET  /webui/<asset>                         — static assets

Per-request state is captured via closures rather than FastAPI's
``Depends`` — the stores are long-lived for the life of the server
and swapping them at runtime isn't a use case we have. Dependency-
injection is available for a later refactor if/when we add an
alternative backend (SQLite, Postgres per the storage refactor).
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from fastapi import APIRouter, FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response

from axiom.extensions.builtins.http import create_app
from axiom.vega.federation.identity import NodeIdentity

if TYPE_CHECKING:
    from axiom.artifacts.registry import ArtifactRegistry

from .broadcast_quizzes import (
    QuizAnswer,
    QuizStore,
    QuizSubmission,
    score_submission,
)
from .classroom_coordinator import coordinator_join_endpoint
from .classroom_interaction import (
    ClassroomInteractionStore,
    InteractionRecord,
)
from .classroom_materials import ClassroomMaterialsStore
from .classroom_threads import (
    Thread,
    ThreadMessage,
    ThreadStore,
    new_thread_id,
    now_iso,
)
from .coordinator_cohort_store import FileCohortStore
from .coordinator_invite_registry import FileInviteRegistry
from .learning_modes import ClassroomModePolicy
from .materials_manifest import (
    build_materials_manifest,
    encode_materials_manifest,
)
from .student_briefs import BriefStore

# ---------------------------------------------------------------------------
# Serialization helpers (shared with legacy handler)
# ---------------------------------------------------------------------------


def _thread_wire(thread: Thread) -> dict:
    return {
        "thread_id": thread.thread_id,
        "classroom_id": thread.classroom_id,
        "student_id": thread.student_id,
        "opened_by": thread.opened_by,
        "status": thread.status,
        "opened_at": thread.opened_at,
        "messages": [asdict(m) for m in thread.messages],
    }


def _quiz_wire(quiz) -> dict:
    return {
        "quiz_id": quiz.quiz_id,
        "classroom_id": quiz.classroom_id,
        "created_at": quiz.created_at,
        "created_by": quiz.created_by,
        "topic": quiz.topic,
        "questions": [asdict(q) for q in quiz.questions],
    }


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def create_classroom_app(
    *,
    coordinator_identity: NodeIdentity,
    classroom_id: str,
    cohort_store: FileCohortStore,
    invite_registry: FileInviteRegistry,
    on_student_joined: Callable[[str], None] | None = None,
    materials_store: ClassroomMaterialsStore | None = None,
    interaction_store: ClassroomInteractionStore | None = None,
    brief_store: BriefStore | None = None,
    thread_store: ThreadStore | None = None,
    quiz_store: QuizStore | None = None,
    artifact_registry: ArtifactRegistry | None = None,
) -> FastAPI:
    """Build the classroom coordinator FastAPI app.

    All stores are optional so an instructor can run a minimal
    coordinator (e.g. just join + materials, no threads yet) and
    unreachable endpoints respond 404 cleanly — same opt-in model the
    legacy handler had.
    """
    app = create_app(
        title=f"Axiom Classroom — {classroom_id}",
        version="0.2.0",
        description="FastAPI coordinator for a single classroom.",
    )

    @app.get("/healthz")
    def _healthz() -> dict:
        return {"ok": True, "classroom_id": classroom_id}

    # -- Join + interaction (core) --
    core = _build_core_router(
        coordinator_identity=coordinator_identity,
        classroom_id=classroom_id,
        cohort_store=cohort_store,
        invite_registry=invite_registry,
        on_student_joined=on_student_joined,
        interaction_store=interaction_store,
    )
    app.include_router(core)

    # -- Policy (always served) --
    app.include_router(_build_policy_router(
        cohort_store=cohort_store, classroom_id=classroom_id,
    ))

    # -- Materials (opt-in) --
    if materials_store is not None:
        app.include_router(_build_materials_router(
            coordinator_identity=coordinator_identity,
            classroom_id=classroom_id,
            materials_store=materials_store,
        ))

    # -- Briefs (opt-in) --
    if brief_store is not None:
        app.include_router(_build_briefs_router(brief_store=brief_store))

    # -- Memory transparency (opt-in, requires interactions) --
    if interaction_store is not None:
        app.include_router(_build_memory_router(
            interaction_store=interaction_store,
        ))

    # -- Recent activity projection (opt-in, requires L1 fragment store)
    # Layer 3 of ADR-033. Mounted only when an artifact_registry is
    # supplied — coordinators running without dual-write fall back to
    # the legacy summary_for_student endpoint above.
    if artifact_registry is not None:
        app.include_router(_build_recent_activity_router(
            classroom_id=classroom_id,
            artifact_registry=artifact_registry,
        ))

    # -- Threads (opt-in) --
    if thread_store is not None:
        app.include_router(_build_threads_router(
            classroom_id=classroom_id, thread_store=thread_store,
        ))

    # -- Quizzes (opt-in) --
    if quiz_store is not None:
        app.include_router(_build_quizzes_router(quiz_store=quiz_store))

    # -- Static landing + webui assets --
    app.include_router(_build_webui_router(classroom_id=classroom_id))

    return app


# ---------------------------------------------------------------------------
# Routers
# ---------------------------------------------------------------------------


def _build_core_router(
    *,
    coordinator_identity: NodeIdentity,
    classroom_id: str,
    cohort_store: FileCohortStore,
    invite_registry: FileInviteRegistry,
    on_student_joined: Callable[[str], None] | None,
    interaction_store: ClassroomInteractionStore | None,
) -> APIRouter:
    router = APIRouter()

    @router.post("/classroom/join")
    async def _join(request: Request):
        body = (await request.body()).decode("utf-8")
        cohort = cohort_store.load(classroom_id)
        status, response_body, updated = coordinator_join_endpoint(
            encoded_request=body,
            coordinator_identity=coordinator_identity,
            cohort=cohort,
            invite_registry=invite_registry,
        )
        if updated is not None:
            cohort_store.save(updated)
            if on_student_joined is not None and updated.members:
                try:
                    on_student_joined(updated.members[-1].student_id)
                except Exception:
                    pass
        return Response(
            content=response_body,
            status_code=status,
            media_type="application/json",
        )

    @router.post("/classroom/interaction")
    async def _interaction(request: Request):
        if interaction_store is None:
            raise HTTPException(status_code=404, detail="not found")
        body = (await request.body()).decode("utf-8")
        try:
            obj = json.loads(body) if body else {}
        except json.JSONDecodeError:
            raise HTTPException(status_code=400, detail="invalid JSON")
        if not isinstance(obj, dict) or not obj.get("student_id"):
            raise HTTPException(status_code=400, detail="missing student_id")
        record = InteractionRecord(
            student_id=str(obj.get("student_id", "")),
            question=str(obj.get("question", "")),
            had_answer=bool(obj.get("had_answer", False)),
            citations_count=int(obj.get("citations_count", 0)),
            timestamp=datetime.now(UTC).isoformat(),
            classroom_id=classroom_id,
            mode=obj.get("mode"),
        )
        try:
            interaction_store.append(record)
        except Exception:
            raise HTTPException(
                status_code=500,
                detail="could not log interaction",
            )
        return {"logged": True}

    return router


def _build_policy_router(
    *,
    cohort_store: FileCohortStore,
    classroom_id: str,
) -> APIRouter:
    router = APIRouter()

    @router.get("/classroom/policy")
    def _policy():
        try:
            raw = cohort_store.get_mode_policy(classroom_id)
        except Exception:
            raw = None
        if raw is None:
            raw = ClassroomModePolicy.default().to_dict()
        return raw

    return router


def _build_materials_router(
    *,
    coordinator_identity: NodeIdentity,
    classroom_id: str,
    materials_store: ClassroomMaterialsStore,
) -> APIRouter:
    router = APIRouter()

    @router.get("/classroom/materials/manifest")
    def _manifest():
        manifest = build_materials_manifest(
            identity=coordinator_identity,
            classroom_id=classroom_id,
            store=materials_store,
        )
        return Response(
            content=encode_materials_manifest(manifest),
            media_type="application/json",
        )

    @router.get("/classroom/materials/{file_id}")
    def _file(file_id: str):
        # Defense-in-depth: FastAPI already unpacks path segments but
        # the file_id shouldn't contain '/' or '..' — guard anyway.
        if "/" in file_id or ".." in file_id:
            raise HTTPException(status_code=404, detail="not found")
        try:
            path = materials_store.get_path(file_id)
        except KeyError:
            raise HTTPException(status_code=404, detail="not found")
        try:
            data = path.read_bytes()
        except OSError:
            raise HTTPException(
                status_code=500, detail="could not read file",
            )
        return Response(content=data, media_type="application/octet-stream")

    return router


def _build_briefs_router(*, brief_store: BriefStore) -> APIRouter:
    router = APIRouter()

    @router.get("/classroom/briefs/{student_id}")
    def _brief(student_id: str):
        if "/" in student_id or not student_id:
            raise HTTPException(status_code=404, detail="not found")
        try:
            brief = brief_store.latest_approved_for_student(student_id)
        except Exception:
            brief = None
        if brief is None:
            raise HTTPException(status_code=404, detail="no brief available")
        return asdict(brief)

    return router


def _build_memory_router(
    *,
    interaction_store: ClassroomInteractionStore,
) -> APIRouter:
    """Memory transparency endpoints — students see what the coordinator
    has logged about them, and can retract specific interactions.

    Both endpoints scope by ``student_id``. Authentication matches the
    rest of the v0 coordinator surface (trust the network within a
    single classroom); a follow-up adds signed requests so retractions
    can't be forged across students."""
    router = APIRouter()

    @router.get("/classroom/memory/{student_id}")
    def _memory(student_id: str):
        if "/" in student_id or not student_id:
            raise HTTPException(status_code=404, detail="not found")
        return interaction_store.summary_for_student(student_id)

    @router.delete("/classroom/memory/{student_id}/{interaction_id}")
    def _forget(student_id: str, interaction_id: str):
        if "/" in student_id or not student_id:
            raise HTTPException(status_code=404, detail="not found")
        if "/" in interaction_id or not interaction_id:
            raise HTTPException(status_code=404, detail="not found")
        result = interaction_store.forget(
            student_id=student_id, interaction_id=interaction_id,
        )
        if not result.get("forgotten"):
            raise HTTPException(
                status_code=404,
                detail=result.get("error", "not found"),
            )
        return result

    return router


def _build_recent_activity_router(
    *,
    classroom_id: str,
    artifact_registry: ArtifactRegistry,
) -> APIRouter:
    """Recent-activity projection endpoint — Layer 3 of ADR-033.

    Backs ``GET /classroom/recent/{student_id}?n=N`` with the generic
    ``RecentActivityProjection`` from ``axiom.memory.projections``.
    The student-side ask path consumes this to fold episodic memory
    into the LLM system prompt (cheapest user-visible win identified
    in the classroom end-to-end review).

    Returns ``{"scope", "principal_id", "fragments": [{...}]}`` —
    flat dict so older clients parse cleanly if fields are added
    later. Empty ``fragments`` is a normal response (no 404).
    """
    from axiom.memory.projections import (
        RecentActivityProjection,
        TaskSpec,
    )

    router = APIRouter()

    @router.get("/classroom/recent/{student_id}")
    def _recent(student_id: str, n: int = 5):
        if "/" in student_id or not student_id:
            raise HTTPException(status_code=404, detail="not found")
        # Bound n to a sane range — defends against pathological
        # values from misbehaving clients.
        window = max(1, min(int(n), 50))
        proj = RecentActivityProjection(
            artifact_registry=artifact_registry,
            window_n=window,
        )
        result = proj.project(
            TaskSpec(task_type="recent_activity", scope=classroom_id),
            principal_id=student_id,
        )
        return {
            "scope": result.scope,
            "principal_id": result.principal_id,
            "fragments": [
                {
                    "id": f.id,
                    "event_time": f.content.get(
                        "event_time", f.provenance.timestamp,
                    ),
                    "question": f.content.get("question", ""),
                    "mode": f.content.get("mode", "ask"),
                    "had_answer": f.content.get("had_answer", False),
                    "interaction_id": f.content.get("interaction_id", ""),
                }
                for f in result.fragments
            ],
        }

    return router


def _build_threads_router(
    *,
    classroom_id: str,
    thread_store: ThreadStore,
) -> APIRouter:
    router = APIRouter()

    @router.post("/classroom/threads/open")
    async def _open(request: Request):
        body = (await request.body()).decode("utf-8")
        try:
            obj = json.loads(body) if body else {}
        except json.JSONDecodeError:
            raise HTTPException(status_code=400, detail="invalid JSON")
        if not isinstance(obj, dict):
            raise HTTPException(status_code=400, detail="body must be object")
        student_id = str(obj.get("student_id", ""))
        opened_by = str(obj.get("opened_by", ""))
        text = str(obj.get("text", ""))
        if (
            not student_id
            or opened_by not in ("instructor", "student")
            or not text
        ):
            raise HTTPException(
                status_code=400,
                detail="missing student_id / opened_by / text",
            )
        author_id = str(obj.get("author_id", student_id))
        ts = now_iso()
        thread = Thread(
            thread_id=new_thread_id(),
            classroom_id=classroom_id,
            student_id=student_id,
            opened_by=opened_by,
            status="open",
            opened_at=ts,
            messages=[ThreadMessage(
                author_role=opened_by,
                author_id=author_id,
                text=text,
                timestamp=ts,
            )],
        )
        thread_store.save(thread)
        return JSONResponse(content=_thread_wire(thread), status_code=201)

    @router.post("/classroom/threads/{thread_id}/reply")
    async def _reply(thread_id: str, request: Request):
        if not thread_id or "/" in thread_id:
            raise HTTPException(status_code=404, detail="not found")
        body = (await request.body()).decode("utf-8")
        try:
            obj = json.loads(body) if body else {}
        except json.JSONDecodeError:
            raise HTTPException(status_code=400, detail="invalid JSON")
        role = str(obj.get("author_role", ""))
        author = str(obj.get("author_id", ""))
        text = str(obj.get("text", ""))
        if role not in ("instructor", "student") or not text:
            raise HTTPException(
                status_code=400,
                detail="missing author_role or text",
            )
        try:
            updated = thread_store.reply(thread_id, ThreadMessage(
                author_role=role, author_id=author,
                text=text, timestamp=now_iso(),
            ))
        except KeyError:
            raise HTTPException(status_code=404, detail="no such thread")
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        return _thread_wire(updated)

    @router.get("/classroom/threads")
    def _list_threads(student: str | None = Query(default=None)):
        if student:
            threads = thread_store.list_for_student(student)
        else:
            threads = thread_store.list_all()
        return {"threads": [_thread_wire(t) for t in threads]}

    @router.get("/classroom/threads/{thread_id}")
    def _fetch_thread(thread_id: str):
        if not thread_id or "/" in thread_id:
            raise HTTPException(status_code=404, detail="not found")
        thread = thread_store.get(thread_id)
        if thread is None:
            raise HTTPException(status_code=404, detail="no such thread")
        return _thread_wire(thread)

    return router


def _build_quizzes_router(*, quiz_store: QuizStore) -> APIRouter:
    router = APIRouter()

    @router.get("/classroom/quizzes/pending")
    def _pending(student: str | None = Query(default=None)):
        if not student:
            raise HTTPException(status_code=400, detail="missing ?student=<id>")
        pending = quiz_store.pending_for_student(student)
        return {"quizzes": [_quiz_wire(q) for q in pending]}

    @router.get("/classroom/quizzes/{quiz_id}")
    def _fetch(quiz_id: str):
        if not quiz_id or "/" in quiz_id:
            raise HTTPException(status_code=404, detail="not found")
        quiz = quiz_store.get(quiz_id)
        if quiz is None:
            raise HTTPException(status_code=404, detail="no such quiz")
        return _quiz_wire(quiz)

    @router.post("/classroom/quizzes/{quiz_id}/submit")
    async def _submit(quiz_id: str, request: Request):
        if not quiz_id or "/" in quiz_id:
            raise HTTPException(status_code=404, detail="not found")
        quiz = quiz_store.get(quiz_id)
        if quiz is None:
            raise HTTPException(status_code=404, detail="no such quiz")
        body = (await request.body()).decode("utf-8")
        try:
            obj = json.loads(body) if body else {}
        except json.JSONDecodeError:
            raise HTTPException(status_code=400, detail="invalid JSON")
        student_id = str(obj.get("student_id", ""))
        if not student_id:
            raise HTTPException(status_code=400, detail="missing student_id")
        raw_answers = obj.get("answers") or []
        if not isinstance(raw_answers, list):
            raise HTTPException(status_code=400, detail="answers must be a list")
        answers: list[QuizAnswer] = []
        for a in raw_answers:
            if not isinstance(a, dict):
                continue
            try:
                answers.append(QuizAnswer(
                    question_index=int(a["question_index"]),
                    answer_text=str(a.get("answer_text", "")),
                ))
            except (KeyError, ValueError):
                continue
        sub = QuizSubmission(
            quiz_id=quiz_id,
            student_id=student_id,
            submitted_at=now_iso(),
            answers=answers,
        )
        quiz_store.save_submission(sub)
        per_q, rate = score_submission(quiz, sub)
        return {
            "quiz_id": quiz_id,
            "student_id": student_id,
            "score": rate,
            "per_question": [
                {
                    "passed": s.passed,
                    "missed_keywords": s.missed_keywords,
                }
                for s in per_q
            ],
        }

    return router


def _build_webui_router(*, classroom_id: str) -> APIRouter:
    router = APIRouter()

    @router.get("/", response_class=HTMLResponse)
    @router.get("/index.html", response_class=HTMLResponse)
    def _landing():
        html_path = Path(__file__).parent / "webui" / "landing.html"
        if not html_path.is_file():
            raise HTTPException(
                status_code=500, detail="landing page not found",
            )
        html = html_path.read_text().replace(
            "{{ CLASSROOM_ID }}", classroom_id,
        )
        return HTMLResponse(content=html)

    @router.get("/webui/{rel:path}")
    def _asset(rel: str):
        # Defense-in-depth: disallow traversal.
        if ".." in rel or rel.startswith("/") or rel.startswith("\\"):
            raise HTTPException(status_code=404, detail="not found")
        path = Path(__file__).parent / "webui" / rel
        try:
            resolved = path.resolve()
            base = (Path(__file__).parent / "webui").resolve()
            if base not in resolved.parents and resolved != base:
                raise HTTPException(status_code=404, detail="not found")
        except OSError:
            raise HTTPException(status_code=404, detail="not found")
        if not path.is_file():
            raise HTTPException(status_code=404, detail="not found")
        ct = "application/octet-stream"
        if rel.endswith(".css"):
            ct = "text/css; charset=utf-8"
        elif rel.endswith(".js"):
            ct = "application/javascript; charset=utf-8"
        elif rel.endswith(".html"):
            ct = "text/html; charset=utf-8"
        elif rel.endswith(".svg"):
            ct = "image/svg+xml"
        try:
            data = path.read_bytes()
        except OSError:
            raise HTTPException(
                status_code=500, detail="could not read asset",
            )
        # Use PlainTextResponse for CSS (so headers + body are right);
        # Response for everything else.
        if ct.startswith("text/"):
            return Response(content=data, media_type=ct)
        return Response(content=data, media_type=ct)

    return router


__all__ = ["create_classroom_app"]
