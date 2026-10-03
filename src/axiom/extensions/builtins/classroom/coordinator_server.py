# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Coordinator HTTP server — the long-running half of the instructor side.

Wraps :func:`coordinator_join_endpoint` in a :class:`BaseHTTPRequestHandler`
with disk-backed registries so instructor state survives restarts and
invites minted in a separate ``axi classroom invite`` process are
picked up without restarting the server.

Each request re-reads the registries from disk. For classroom-sized
cohorts this is cheap (small JSON files) and removes the need for IPC
between the mint and serve commands.

Routes:
    POST /classroom/join                        — student join ceremony
    GET  /classroom/materials/manifest          — signed list of files
    GET  /classroom/materials/<file_id>         — raw file content

The materials endpoints are opt-in: pass ``materials_store`` to
:func:`make_coordinator_handler` to enable them. If absent, both
materials paths return 404 cleanly (older servers don't break).
"""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC
from http.server import BaseHTTPRequestHandler
from pathlib import Path

from axiom.vega.federation.identity import NodeIdentity

from .broadcast_quizzes import (
    QuizAnswer,
    QuizStore,
    QuizSubmission,
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
from .materials_manifest import (
    build_materials_manifest,
    encode_materials_manifest,
)
from .student_briefs import BriefStore


def make_coordinator_handler(
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
) -> type[BaseHTTPRequestHandler]:
    """Build a handler class bound to this instructor's state.

    Returns a `BaseHTTPRequestHandler` subclass; a caller passes it to
    `HTTPServer((host, port), handler_cls)`. The handler re-reads the
    cohort + registry on every request, so updates from a sibling
    mint process are picked up immediately.

    ``on_student_joined`` fires with the student_id after each
    successful join — the CLI uses it to print a live one-liner so
    the instructor sees arrivals without leaving the terminal.

    ``materials_store`` enables the read-only materials GET endpoints.
    Pass None (the default) to keep the legacy join-only behaviour.
    """

    class _CoordinatorHandler(BaseHTTPRequestHandler):
        def log_message(self, format, *args):  # noqa: A002 — stdlib signature
            return

        # --- POST: join ceremony ------------------------------------------------

        def do_POST(self):  # noqa: N802 — stdlib signature
            if self.path == "/classroom/join":
                self._handle_join()
                return
            if self.path == "/classroom/interaction":
                self._handle_interaction()
                return
            if self.path == "/classroom/threads/open":
                self._handle_thread_open()
                return
            if self.path.startswith("/classroom/threads/") and self.path.endswith("/reply"):
                thread_id = self.path[len("/classroom/threads/"):-len("/reply")]
                self._handle_thread_reply(thread_id)
                return
            if self.path.startswith("/classroom/quizzes/") and self.path.endswith("/submit"):
                qid = self.path[len("/classroom/quizzes/"):-len("/submit")]
                self._handle_quiz_submit(qid)
                return
            self._send_error(404, "not found")

        def _handle_join(self) -> None:
            length = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(length).decode("utf-8") if length else ""

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

            self._respond(status, response_body.encode("utf-8"), "application/json")

        def _handle_interaction(self) -> None:
            """Accept a plain-JSON interaction report from a student.

            No signature verification yet — the threat model is "a
            cohort member ran `axi classroom ask` and the system
            logged it", not "anonymous internet user tries to
            poison the log". A future PR can layer Ed25519 signing
            + rejection of non-member student_ids.
            """
            if interaction_store is None:
                self._send_error(404, "not found")
                return
            length = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(length).decode("utf-8") if length else ""
            try:
                obj = json.loads(body) if body else {}
            except json.JSONDecodeError:
                self._send_error(400, "invalid JSON")
                return
            if not isinstance(obj, dict) or not obj.get("student_id"):
                self._send_error(400, "missing student_id")
                return
            from datetime import datetime
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
                self._send_error(500, "could not log interaction")
                return
            self._respond(200, b'{"logged": true}', "application/json")

        # --- GET: materials -----------------------------------------------------

        def do_GET(self):  # noqa: N802 — stdlib signature
            # Static web surface (D1) — landing page and CSS.
            if self.path == "/" or self.path == "/index.html":
                self._handle_landing()
                return
            if self.path.startswith("/webui/"):
                self._handle_webui_asset(self.path[len("/webui/"):])
                return

            # `/classroom/policy` is served regardless of whether
            # materials are enabled — it's cheap + always safe to
            # read, and students fetch it even in cite-only flows.
            if self.path == "/classroom/policy":
                self._handle_policy()
                return

            # Student brief fetch. Returns only approved briefs; drafts
            # stay invisible until the instructor explicitly approves.
            brief_prefix = "/classroom/briefs/"
            if self.path.startswith(brief_prefix) and brief_store is not None:
                self._handle_brief_fetch(self.path[len(brief_prefix):])
                return

            # Thread list / fetch — students see their own, instructor
            # can fetch anyone's via the same endpoint (trust model
            # matches interaction + brief endpoints).
            if thread_store is not None:
                if self.path.startswith("/classroom/threads?"):
                    self._handle_threads_list()
                    return
                if self.path == "/classroom/threads":
                    self._handle_threads_list()
                    return
                if self.path.startswith("/classroom/threads/"):
                    thread_id = self.path[len("/classroom/threads/"):]
                    self._handle_thread_fetch(thread_id)
                    return

            # Quiz endpoints.
            if quiz_store is not None:
                if self.path.startswith("/classroom/quizzes/pending"):
                    self._handle_quizzes_pending()
                    return
                if self.path.startswith("/classroom/quizzes/"):
                    qid = self.path[len("/classroom/quizzes/"):]
                    self._handle_quiz_fetch(qid)
                    return

            if materials_store is None:
                self._send_error(404, "not found")
                return

            if self.path == "/classroom/materials/manifest":
                manifest = build_materials_manifest(
                    identity=coordinator_identity,
                    classroom_id=classroom_id,
                    store=materials_store,
                )
                body = encode_materials_manifest(manifest).encode("utf-8")
                self._respond(200, body, "application/json")
                return

            prefix = "/classroom/materials/"
            if self.path.startswith(prefix):
                file_id = self.path[len(prefix):]
                # Guard against path traversal: file_id must not contain
                # slashes. `get_path` also raises KeyError for unknown
                # ids, which covers the common case.
                if "/" in file_id or ".." in file_id:
                    self._send_error(404, "not found")
                    return
                try:
                    path = materials_store.get_path(file_id)
                except KeyError:
                    self._send_error(404, "not found")
                    return
                try:
                    data = path.read_bytes()
                except OSError:
                    self._send_error(500, "could not read file")
                    return
                self._respond(200, data, "application/octet-stream")
                return

            self._send_error(404, "not found")

        def _handle_landing(self) -> None:
            """Serve the per-class landing page.

            Substitutes ``{{ CLASSROOM_ID }}`` at request time so the
            page feels specific to whatever class this coordinator is
            serving (no per-class build step).
            """
            html_path = (
                Path(__file__).parent / "webui" / "landing.html"
            )
            if not html_path.is_file():
                self._send_error(500, "landing page not found")
                return
            html = html_path.read_text().replace(
                "{{ CLASSROOM_ID }}", classroom_id,
            )
            self._respond(200, html.encode("utf-8"), "text/html; charset=utf-8")

        def _handle_webui_asset(self, rel: str) -> None:
            """Serve static assets under ``webui/`` (CSS/JS for now)."""
            # Defense-in-depth: disallow anything with traversal.
            if ".." in rel or rel.startswith("/") or rel.startswith("\\"):
                self._send_error(404, "not found")
                return
            path = Path(__file__).parent / "webui" / rel
            try:
                resolved = path.resolve()
                base = (Path(__file__).parent / "webui").resolve()
                if base not in resolved.parents and resolved != base:
                    self._send_error(404, "not found")
                    return
            except OSError:
                self._send_error(404, "not found")
                return
            if not path.is_file():
                self._send_error(404, "not found")
                return
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
                self._send_error(500, "could not read asset")
                return
            self._respond(200, data, ct)

        def _handle_quizzes_pending(self) -> None:
            """Return quizzes the student hasn't submitted yet."""
            if quiz_store is None:
                self._send_error(404, "not found")
                return
            import urllib.parse as _up
            qs = self.path.split("?", 1)[1] if "?" in self.path else ""
            params = _up.parse_qs(qs) if qs else {}
            student_id = (params.get("student") or [None])[0]
            if not student_id:
                self._send_error(400, "missing ?student=<id>")
                return
            pending = quiz_store.pending_for_student(student_id)
            body = json.dumps({
                "quizzes": [_quiz_to_dict_for_wire(q) for q in pending],
            }).encode("utf-8")
            self._respond(200, body, "application/json")

        def _handle_quiz_fetch(self, quiz_id: str) -> None:
            if quiz_store is None:
                self._send_error(404, "not found")
                return
            if not quiz_id or "/" in quiz_id or "?" in quiz_id:
                self._send_error(404, "not found")
                return
            quiz = quiz_store.get(quiz_id)
            if quiz is None:
                self._send_error(404, "no such quiz")
                return
            body = json.dumps(_quiz_to_dict_for_wire(quiz)).encode("utf-8")
            self._respond(200, body, "application/json")

        def _handle_quiz_submit(self, quiz_id: str) -> None:
            """Accept a student's answers. Idempotent — re-submits
            overwrite the prior submission for that student."""
            if quiz_store is None:
                self._send_error(404, "not found")
                return
            if not quiz_id or "/" in quiz_id:
                self._send_error(404, "not found")
                return
            quiz = quiz_store.get(quiz_id)
            if quiz is None:
                self._send_error(404, "no such quiz")
                return
            length = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(length).decode("utf-8") if length else ""
            try:
                obj = json.loads(body) if body else {}
            except json.JSONDecodeError:
                self._send_error(400, "invalid JSON")
                return
            student_id = str(obj.get("student_id", ""))
            if not student_id:
                self._send_error(400, "missing student_id")
                return
            raw_answers = obj.get("answers") or []
            if not isinstance(raw_answers, list):
                self._send_error(400, "answers must be a list")
                return
            answers = []
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
            # Score for the student — immediate feedback.
            from .broadcast_quizzes import score_submission
            per_q, rate = score_submission(quiz, sub)
            self._respond(
                200,
                json.dumps({
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
                }).encode("utf-8"),
                "application/json",
            )

        def _handle_thread_open(self) -> None:
            """Open a new thread from an HTTP caller.

            Body: ``{student_id, opened_by, author_id, text}``.
            Creates a fresh thread and appends the first message.
            Returns the full thread JSON.
            """
            if thread_store is None:
                self._send_error(404, "not found")
                return
            length = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(length).decode("utf-8") if length else ""
            try:
                obj = json.loads(body) if body else {}
            except json.JSONDecodeError:
                self._send_error(400, "invalid JSON")
                return
            if not isinstance(obj, dict):
                self._send_error(400, "body must be object")
                return
            student_id = str(obj.get("student_id", ""))
            opened_by = str(obj.get("opened_by", ""))
            text = str(obj.get("text", ""))
            if not student_id or opened_by not in ("instructor", "student") or not text:
                self._send_error(400, "missing student_id / opened_by / text")
                return
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
            self._respond(
                201,
                json.dumps(_thread_to_dict_for_wire(thread)).encode("utf-8"),
                "application/json",
            )

        def _handle_thread_reply(self, thread_id: str) -> None:
            if thread_store is None:
                self._send_error(404, "not found")
                return
            if not thread_id or "/" in thread_id:
                self._send_error(404, "not found")
                return
            length = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(length).decode("utf-8") if length else ""
            try:
                obj = json.loads(body) if body else {}
            except json.JSONDecodeError:
                self._send_error(400, "invalid JSON")
                return
            role = str(obj.get("author_role", ""))
            author = str(obj.get("author_id", ""))
            text = str(obj.get("text", ""))
            if role not in ("instructor", "student") or not text:
                self._send_error(400, "missing author_role or text")
                return
            try:
                updated = thread_store.reply(thread_id, ThreadMessage(
                    author_role=role, author_id=author,
                    text=text, timestamp=now_iso(),
                ))
            except KeyError:
                self._send_error(404, "no such thread")
                return
            except ValueError as exc:
                self._send_error(400, str(exc))
                return
            self._respond(
                200,
                json.dumps(_thread_to_dict_for_wire(updated)).encode("utf-8"),
                "application/json",
            )

        def _handle_threads_list(self) -> None:
            if thread_store is None:
                self._send_error(404, "not found")
                return
            import urllib.parse as _up
            qs = self.path.split("?", 1)[1] if "?" in self.path else ""
            params = _up.parse_qs(qs) if qs else {}
            student_id = (params.get("student") or [None])[0]
            if student_id:
                threads = thread_store.list_for_student(student_id)
            else:
                threads = thread_store.list_all()
            body = json.dumps({
                "threads": [_thread_to_dict_for_wire(t) for t in threads],
            }).encode("utf-8")
            self._respond(200, body, "application/json")

        def _handle_thread_fetch(self, thread_id: str) -> None:
            if thread_store is None:
                self._send_error(404, "not found")
                return
            if not thread_id or "/" in thread_id or "?" in thread_id:
                self._send_error(404, "not found")
                return
            thread = thread_store.get(thread_id)
            if thread is None:
                self._send_error(404, "no such thread")
                return
            body = json.dumps(_thread_to_dict_for_wire(thread)).encode("utf-8")
            self._respond(200, body, "application/json")

        def _handle_brief_fetch(self, student_id_enc: str) -> None:
            """Return the student's latest APPROVED brief, or 404.

            Drafts are deliberately invisible over HTTP — the
            instructor-curation loop is the point. No auth yet; in
            a trusted cohort this is acceptable (see design note in
            student_briefs.py). A follow-up can require a signed
            request from the student's membership key.
            """
            import urllib.parse as _up
            student_id = _up.unquote(student_id_enc)
            if "/" in student_id or not student_id:
                self._send_error(404, "not found")
                return
            from dataclasses import asdict as _asdict
            try:
                brief = brief_store.latest_approved_for_student(student_id)
            except Exception:
                brief = None
            if brief is None:
                self._send_error(404, "no brief available")
                return
            body = json.dumps(_asdict(brief)).encode("utf-8")
            self._respond(200, body, "application/json")

        def _handle_policy(self) -> None:
            """Serve the classroom's mode policy (JSON).

            Students fetch this at ask time to know which modes they may
            use. Returns default policy if none stored — same as the
            wire contract on the student side.
            """
            try:
                raw = cohort_store.get_mode_policy(classroom_id)
            except Exception:
                raw = None
            if raw is None:
                from .learning_modes import ClassroomModePolicy

                raw = ClassroomModePolicy.default().to_dict()
            body = json.dumps(raw).encode("utf-8")
            self._respond(200, body, "application/json")

        # --- helpers ------------------------------------------------------------

        def _respond(self, status: int, body: bytes, content_type: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _send_error(self, status: int, message: str) -> None:
            body = message.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    return _CoordinatorHandler


def _quiz_to_dict_for_wire(quiz) -> dict:
    from dataclasses import asdict as _asdict
    return {
        "quiz_id": quiz.quiz_id,
        "classroom_id": quiz.classroom_id,
        "created_at": quiz.created_at,
        "created_by": quiz.created_by,
        "topic": quiz.topic,
        "questions": [_asdict(q) for q in quiz.questions],
    }


def _thread_to_dict_for_wire(thread: Thread) -> dict:
    """Serialize a Thread for HTTP response bodies."""
    from dataclasses import asdict as _asdict
    return {
        "thread_id": thread.thread_id,
        "classroom_id": thread.classroom_id,
        "student_id": thread.student_id,
        "opened_by": thread.opened_by,
        "status": thread.status,
        "opened_at": thread.opened_at,
        "messages": [_asdict(m) for m in thread.messages],
    }


__all__ = ["make_coordinator_handler"]
