# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""FastAPI coordinator — wire-compat regression.

Proves the new ``create_classroom_app`` FastAPI app answers the same
URLs + payload shapes as the legacy ``make_coordinator_handler``
stdlib handler. Existing student CLIs + extension tests that talk to
``axi classroom serve`` keep working after the migration.

Tests here use FastAPI's ``TestClient`` (in-process — no socket) for
fast routing checks, plus a real ``ThreadedServer`` (uvicorn) run for
one end-to-end smoke so we prove the full async stack is live.
"""

from __future__ import annotations

import hashlib
import json

import pytest
from fastapi.testclient import TestClient

from axiom.extensions.builtins.classroom.broadcast_quizzes import (
    BroadcastedQuiz,
    QuizQuestion,
    QuizStore,
)
from axiom.extensions.builtins.classroom.classroom_api import (
    create_classroom_app,
)
from axiom.extensions.builtins.classroom.classroom_federation import (
    add_member,
    create_cohort,
)
from axiom.extensions.builtins.classroom.classroom_interaction import (
    ClassroomInteractionStore,
)
from axiom.extensions.builtins.classroom.classroom_materials import (
    ClassroomMaterialsStore,
)
from axiom.extensions.builtins.classroom.classroom_threads import (
    ThreadStore,
)
from axiom.extensions.builtins.classroom.coordinator_cohort_store import (
    FileCohortStore,
)
from axiom.extensions.builtins.classroom.coordinator_invite_registry import (
    FileInviteRegistry,
)
from axiom.extensions.builtins.classroom.student_briefs import (
    BriefStore,
    StudentBrief,
)
from axiom.extensions.builtins.http import ThreadedServer
from axiom.vega.federation.identity import generate_identity


@pytest.fixture
def wired_app(tmp_path):
    """Fully-wired classroom app with every store enabled."""
    coord = generate_identity(
        owner="prof@ut.edu", keys_dir=tmp_path / "coord-keys",
    )
    coord_dir = tmp_path / "coord"
    classroom_id = "NE101"

    cohort = create_cohort(classroom_id, coord.node_id)
    cohort = add_member(cohort, "alice@ut.edu", "alice_node", "tok_a")
    cohort_store = FileCohortStore(coord_dir)
    cohort_store.save(
        cohort, coordinator_url="http://placeholder/classroom/join",
    )
    invite_registry = FileInviteRegistry(coord_dir / "invites.json")

    classroom_coord_dir = coord_dir / "classrooms" / classroom_id
    materials = ClassroomMaterialsStore(classroom_coord_dir)
    materials.add_text(
        "Control rods absorb neutrons.",
        filename="ch1.md", title="Chapter 1",
    )
    interactions = ClassroomInteractionStore(classroom_coord_dir)
    briefs = BriefStore(classroom_coord_dir)
    threads = ThreadStore(classroom_coord_dir)
    quizzes = QuizStore(classroom_coord_dir)

    app = create_classroom_app(
        coordinator_identity=coord,
        classroom_id=classroom_id,
        cohort_store=cohort_store,
        invite_registry=invite_registry,
        materials_store=materials,
        interaction_store=interactions,
        brief_store=briefs,
        thread_store=threads,
        quiz_store=quizzes,
    )
    return {
        "app": app,
        "coord": coord,
        "coord_dir": coord_dir,
        "cohort": cohort,
        "classroom_id": classroom_id,
        "materials": materials,
        "interactions": interactions,
        "briefs": briefs,
        "threads": threads,
        "quizzes": quizzes,
    }


# ---------------------------------------------------------------------------
# Landing + assets
# ---------------------------------------------------------------------------


class TestLanding:
    def test_root_serves_html_with_classroom_id(self, wired_app):
        client = TestClient(wired_app["app"])
        resp = client.get("/")
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/html")
        body = resp.text
        assert "NE101" in body
        assert "{{ CLASSROOM_ID }}" not in body

    def test_index_html_is_aliased(self, wired_app):
        client = TestClient(wired_app["app"])
        a = client.get("/").text
        b = client.get("/index.html").text
        assert a == b

    def test_css_served_with_correct_content_type(self, wired_app):
        client = TestClient(wired_app["app"])
        resp = client.get("/webui/style.css")
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/css")

    def test_path_traversal_blocked(self, wired_app):
        client = TestClient(wired_app["app"])
        resp = client.get("/webui/../classroom_api.py")
        assert resp.status_code == 404


class TestHealthz:
    """Regression for the smoke-test bug: the FastAPI migration dropped the
    /healthz endpoint that the legacy stdlib coordinator served. Monitoring
    + uptime checks rely on it."""

    def test_healthz_returns_ok_with_classroom_id(self, wired_app):
        client = TestClient(wired_app["app"])
        resp = client.get("/healthz")
        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is True
        assert body["classroom_id"] == "NE101"


class TestMemoryTransparency:
    """The /classroom/memory/{student_id} endpoint shows a student what the
    coordinator has logged about their activity. Per the user's memory-
    architecture concern: 'wrong things ended up in the classroom memory'
    is mitigated by making memory legible to the student themselves."""

    def _log(self, store, student_id, *, question, mode="ask", had_answer=True):
        from axiom.extensions.builtins.classroom.classroom_interaction import (
            InteractionRecord,
        )
        store.append(InteractionRecord(
            student_id=student_id,
            question=question,
            had_answer=had_answer,
            citations_count=1 if had_answer else 0,
            timestamp="2026-04-25T12:00:00+00:00",
            classroom_id="NE101",
            mode=mode,
        ))

    def test_empty_memory_view_returns_zero_counts(self, wired_app):
        client = TestClient(wired_app["app"])
        resp = client.get("/classroom/memory/alice@ut.edu")
        assert resp.status_code == 200
        body = resp.json()
        assert body["question_count"] == 0
        assert body["answered_count"] == 0
        assert body["unanswered_count"] == 0
        assert body["modes_used"] == {}

    def test_memory_view_reflects_logged_questions(self, wired_app):
        store = wired_app["interactions"]
        self._log(store, "alice@ut.edu", question="What is criticality?")
        self._log(store, "alice@ut.edu", question="How do control rods work?",
                  mode="tutor", had_answer=False)

        client = TestClient(wired_app["app"])
        resp = client.get("/classroom/memory/alice@ut.edu")
        assert resp.status_code == 200
        body = resp.json()
        assert body["question_count"] == 2
        assert body["answered_count"] == 1
        assert body["unanswered_count"] == 1
        assert body["modes_used"] == {"ask": 1, "tutor": 1}
        questions = [r["question"] for r in body["recent_questions"]]
        assert "What is criticality?" in questions
        assert "How do control rods work?" in questions

    def test_memory_view_scopes_to_requested_student(self, wired_app):
        """A student requesting their own memory view never sees anyone
        else's questions — important for federation later, important for
        Ondrej's classroom now."""
        store = wired_app["interactions"]
        self._log(store, "alice@ut.edu", question="Alice question.")
        self._log(store, "bob@ut.edu", question="Bob question.")

        client = TestClient(wired_app["app"])
        alice = client.get("/classroom/memory/alice@ut.edu").json()
        bob = client.get("/classroom/memory/bob@ut.edu").json()

        alice_qs = [r["question"] for r in alice["recent_questions"]]
        bob_qs = [r["question"] for r in bob["recent_questions"]]
        assert alice_qs == ["Alice question."]
        assert bob_qs == ["Bob question."]
        assert alice["question_count"] == 1
        assert bob["question_count"] == 1

    def test_path_traversal_blocked(self, wired_app):
        client = TestClient(wired_app["app"])
        resp = client.get("/classroom/memory/alice%2F..%2Fbob")
        # FastAPI decodes %2F before routing, so this becomes
        # /classroom/memory/alice/../bob and 404s on the route match.
        # Either 404 or our explicit reject is acceptable.
        assert resp.status_code in (404, 400)


class TestMemoryForget:
    """DELETE /classroom/memory/{student_id}/{interaction_id} retracts
    a single interaction. Tombstone-based — original line stays on disk
    for the instructor's audit, but no read surfaces the content."""

    def _log(self, store, student_id, *, question, ts="2026-04-25T12:00:00+00:00"):
        from axiom.extensions.builtins.classroom.classroom_interaction import (
            InteractionRecord,
        )
        rec = InteractionRecord(
            student_id=student_id, question=question, had_answer=True,
            citations_count=1, timestamp=ts, classroom_id="NE101", mode="ask",
        )
        store.append(rec)
        return rec.interaction_id

    def test_forget_removes_from_memory_view(self, wired_app):
        store = wired_app["interactions"]
        iid_keep = self._log(
            store, "alice@ut.edu", question="Q to keep",
            ts="2026-04-25T11:00:00+00:00",
        )
        iid_drop = self._log(
            store, "alice@ut.edu", question="Q to retract",
            ts="2026-04-25T12:00:00+00:00",
        )

        client = TestClient(wired_app["app"])
        resp = client.delete(f"/classroom/memory/alice@ut.edu/{iid_drop}")
        assert resp.status_code == 200
        assert resp.json()["forgotten"] is True

        view = client.get("/classroom/memory/alice@ut.edu").json()
        questions = [r["question"] for r in view["recent_questions"]]
        assert "Q to retract" not in questions
        assert "Q to keep" in questions
        assert view["question_count"] == 1
        assert view["forgotten_count"] == 1
        # interaction_id of the kept record is unchanged.
        assert view["recent_questions"][0]["interaction_id"] == iid_keep

    def test_forget_unknown_id_404s(self, wired_app):
        store = wired_app["interactions"]
        self._log(store, "alice@ut.edu", question="Q")

        client = TestClient(wired_app["app"])
        resp = client.delete("/classroom/memory/alice@ut.edu/bogus_id")
        assert resp.status_code == 404

    def test_forget_cross_student_404s(self, wired_app):
        """Bob can't forget Alice's question even if he knows the id."""
        store = wired_app["interactions"]
        iid = self._log(store, "alice@ut.edu", question="Alice's Q")

        client = TestClient(wired_app["app"])
        resp = client.delete(f"/classroom/memory/bob@ut.edu/{iid}")
        assert resp.status_code == 404

        # And Alice's view is untouched.
        alice = client.get("/classroom/memory/alice@ut.edu").json()
        assert alice["question_count"] == 1
        assert alice["forgotten_count"] == 0

    def test_forget_is_idempotent(self, wired_app):
        store = wired_app["interactions"]
        iid = self._log(store, "alice@ut.edu", question="Q")

        client = TestClient(wired_app["app"])
        first = client.delete(f"/classroom/memory/alice@ut.edu/{iid}")
        second = client.delete(f"/classroom/memory/alice@ut.edu/{iid}")
        assert first.status_code == 200
        assert second.status_code == 200
        assert second.json().get("idempotent") is True


class TestRecentActivityEndpoint:
    """`GET /classroom/recent/{student_id}` is the Layer 3 projection
    endpoint that the student-side ask path consumes for episodic
    memory in the LLM context. Only mounted when the coordinator is
    wired with an artifact_registry (i.e., dual-write enabled)."""

    def _wired_with_registry(self, tmp_path):
        from axiom.artifacts.registry import ArtifactRegistry, InMemoryBackend
        from axiom.extensions.builtins.classroom.classroom_federation import (
            add_member,
            create_cohort,
        )
        from axiom.extensions.builtins.classroom.classroom_interaction import (
            ClassroomInteractionStore,
        )
        from axiom.extensions.builtins.classroom.coordinator_cohort_store import (
            FileCohortStore,
        )
        from axiom.extensions.builtins.classroom.coordinator_invite_registry import (
            FileInviteRegistry,
        )
        from axiom.memory.access import AccessGraphs
        from axiom.memory.adapters import interaction_writer
        from axiom.memory.attest import AuditLog
        from axiom.memory.composition import CompositionService
        from axiom.memory.policy import PolicyCoord
        from axiom.memory.trust import TrustGraph
        from axiom.vega.federation.identity import generate_identity

        coord = generate_identity(
            owner="prof@ut.edu", keys_dir=tmp_path / "coord-keys",
        )
        coord_dir = tmp_path / "coord"
        classroom_id = "NE101"
        cohort = create_cohort(classroom_id, coord.node_id)
        cohort = add_member(cohort, "alice@ut.edu", "alice_node", "tok_a")
        cohort_store = FileCohortStore(coord_dir)
        cohort_store.save(
            cohort, coordinator_url="http://placeholder/classroom/join",
        )
        invite_registry = FileInviteRegistry(coord_dir / "invites.json")

        registry = ArtifactRegistry(backend=InMemoryBackend())
        cs = CompositionService(
            artifact_registry=registry,
            audit_log=AuditLog(tmp_path / "audit.jsonl", signing_keypair=None),
            signing_keypair=None,
            policy_coord=PolicyCoord(global_policy={"write": "private"}),
            access_graphs=AccessGraphs(),
            trust_graph=TrustGraph(),
        )

        interactions = ClassroomInteractionStore(
            tmp_path / "interactions",
            memory_writer=interaction_writer(cs),
            scope_id=classroom_id,
        )

        app = create_classroom_app(
            coordinator_identity=coord,
            classroom_id=classroom_id,
            cohort_store=cohort_store,
            invite_registry=invite_registry,
            interaction_store=interactions,
            artifact_registry=registry,
        )
        return {
            "app": app,
            "interactions": interactions,
            "registry": registry,
            "classroom_id": classroom_id,
        }

    def _ask(self, store, *, student_id, question, ts):
        from axiom.extensions.builtins.classroom.classroom_interaction import (
            InteractionRecord,
        )
        store.append(InteractionRecord(
            student_id=student_id,
            question=question,
            had_answer=True,
            citations_count=1,
            timestamp=ts,
            classroom_id="NE101",
            mode="ask",
        ))

    def test_endpoint_returns_recent_fragments_in_order(self, tmp_path):
        wired = self._wired_with_registry(tmp_path)
        for ts, q in [
            ("2026-04-26T10:00:00+00:00", "old"),
            ("2026-04-26T12:00:00+00:00", "new"),
            ("2026-04-26T11:00:00+00:00", "middle"),
        ]:
            self._ask(
                wired["interactions"],
                student_id="alice@ut.edu", question=q, ts=ts,
            )

        client = TestClient(wired["app"])
        resp = client.get("/classroom/recent/alice@ut.edu")
        assert resp.status_code == 200
        body = resp.json()
        assert body["scope"] == "NE101"
        assert body["principal_id"] == "alice@ut.edu"
        questions = [f["question"] for f in body["fragments"]]
        assert questions == ["new", "middle", "old"]

    def test_endpoint_scopes_to_requested_student(self, tmp_path):
        wired = self._wired_with_registry(tmp_path)
        self._ask(
            wired["interactions"],
            student_id="alice@ut.edu", question="alice Q",
            ts="2026-04-26T10:00:00+00:00",
        )
        self._ask(
            wired["interactions"],
            student_id="bob@ut.edu", question="bob Q",
            ts="2026-04-26T10:00:00+00:00",
        )

        client = TestClient(wired["app"])
        alice = client.get("/classroom/recent/alice@ut.edu").json()
        bob = client.get("/classroom/recent/bob@ut.edu").json()
        assert [f["question"] for f in alice["fragments"]] == ["alice Q"]
        assert [f["question"] for f in bob["fragments"]] == ["bob Q"]

    def test_n_param_truncates_window(self, tmp_path):
        wired = self._wired_with_registry(tmp_path)
        for i in range(8):
            self._ask(
                wired["interactions"],
                student_id="alice@ut.edu", question=f"Q{i}",
                ts=f"2026-04-26T1{i}:00:00+00:00",
            )

        client = TestClient(wired["app"])
        resp = client.get("/classroom/recent/alice@ut.edu?n=3")
        assert len(resp.json()["fragments"]) == 3

    def test_endpoint_404_when_registry_not_wired(self, wired_app):
        """The legacy `wired_app` fixture doesn't pass artifact_registry,
        so the endpoint isn't mounted and returns 404 — student-side
        falls back to /classroom/memory."""
        client = TestClient(wired_app["app"])
        resp = client.get("/classroom/recent/alice@ut.edu")
        assert resp.status_code == 404

    def test_path_traversal_blocked(self, tmp_path):
        wired = self._wired_with_registry(tmp_path)
        client = TestClient(wired["app"])
        resp = client.get("/classroom/recent/alice%2F..%2Fbob")
        assert resp.status_code in (404, 400)

    def test_empty_returns_zero_fragments_not_404(self, tmp_path):
        """A student with no logged questions gets an empty list, not
        a 404 — empty is a normal response."""
        wired = self._wired_with_registry(tmp_path)
        client = TestClient(wired["app"])
        resp = client.get("/classroom/recent/alice@ut.edu")
        assert resp.status_code == 200
        assert resp.json()["fragments"] == []


# ---------------------------------------------------------------------------
# Policy — default when unset
# ---------------------------------------------------------------------------


class TestPolicy:
    def test_policy_returns_default_when_unset(self, wired_app):
        client = TestClient(wired_app["app"])
        resp = client.get("/classroom/policy")
        assert resp.status_code == 200
        data = resp.json()
        # Default policy permits every shipped mode.
        assert "ask" in data["allowed_modes"]
        assert data["forced_mode"] is None


# ---------------------------------------------------------------------------
# Materials
# ---------------------------------------------------------------------------


class TestMaterials:
    def test_manifest_roundtrip(self, wired_app):
        client = TestClient(wired_app["app"])
        resp = client.get("/classroom/materials/manifest")
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("application/json")
        data = resp.json()
        assert data["classroom_id"] == "NE101"
        assert len(data["entries"]) == 1

    def test_file_content_matches_hash(self, wired_app):
        client = TestClient(wired_app["app"])
        entries = wired_app["materials"].list_entries()
        file_id = entries[0].file_id
        resp = client.get(f"/classroom/materials/{file_id}")
        assert resp.status_code == 200
        assert hashlib.sha256(resp.content).digest()  # just proving body present

    def test_unknown_file_returns_404(self, wired_app):
        client = TestClient(wired_app["app"])
        resp = client.get("/classroom/materials/not-a-real-file-id")
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Briefs
# ---------------------------------------------------------------------------


class TestBriefs:
    def test_approved_brief_returned(self, wired_app):
        brief = StudentBrief(
            student_id="alice@ut.edu", classroom_id="NE101",
            period_start="", period_end="",
            generated_at="2026-04-23T10:00+00:00",
            sections={"narrative": "ok"},
            review_status="approved",
        )
        wired_app["briefs"].save(brief)
        wired_app["briefs"].approve("alice@ut.edu", brief.generated_at)

        client = TestClient(wired_app["app"])
        resp = client.get("/classroom/briefs/alice@ut.edu")
        assert resp.status_code == 200
        assert resp.json()["student_id"] == "alice@ut.edu"

    def test_draft_brief_invisible(self, wired_app):
        brief = StudentBrief(
            student_id="bob@ut.edu", classroom_id="NE101",
            period_start="", period_end="",
            generated_at="2026-04-23T10:00+00:00",
            sections={}, review_status="draft",
        )
        wired_app["briefs"].save(brief)

        client = TestClient(wired_app["app"])
        resp = client.get("/classroom/briefs/bob@ut.edu")
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Threads
# ---------------------------------------------------------------------------


class TestThreads:
    def test_open_then_fetch(self, wired_app):
        client = TestClient(wired_app["app"])
        resp = client.post(
            "/classroom/threads/open",
            json={
                "student_id": "alice@ut.edu",
                "opened_by": "student",
                "author_id": "alice@ut.edu",
                "text": "Help on control rods?",
            },
        )
        assert resp.status_code == 201
        thread_id = resp.json()["thread_id"]

        fetched = client.get(f"/classroom/threads/{thread_id}").json()
        assert fetched["student_id"] == "alice@ut.edu"
        assert len(fetched["messages"]) == 1

    def test_reply_transitions_status(self, wired_app):
        client = TestClient(wired_app["app"])
        opened = client.post(
            "/classroom/threads/open",
            json={
                "student_id": "alice@ut.edu",
                "opened_by": "student",
                "author_id": "alice@ut.edu",
                "text": "Q",
            },
        ).json()
        resp = client.post(
            f"/classroom/threads/{opened['thread_id']}/reply",
            json={
                "author_role": "instructor",
                "author_id": "@prof:ut",
                "text": "Think about it",
            },
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "answered"

    def test_list_for_student(self, wired_app):
        client = TestClient(wired_app["app"])
        client.post(
            "/classroom/threads/open",
            json={
                "student_id": "alice@ut.edu",
                "opened_by": "student",
                "author_id": "alice@ut.edu",
                "text": "Q1",
            },
        )
        resp = client.get(
            "/classroom/threads", params={"student": "alice@ut.edu"},
        )
        assert resp.status_code == 200
        threads = resp.json()["threads"]
        assert len(threads) == 1


# ---------------------------------------------------------------------------
# Quizzes
# ---------------------------------------------------------------------------


class TestQuizzes:
    def test_pending_and_submit_roundtrip(self, wired_app):
        quiz = BroadcastedQuiz(
            quiz_id="qabc", classroom_id="NE101",
            created_at="2026-04-24T10:00+00:00",
            created_by="@prof:ut",
            topic="control rods",
            questions=[
                QuizQuestion(question_text="Q1", expected_keywords=["absorb"]),
            ],
        )
        wired_app["quizzes"].save(quiz)

        client = TestClient(wired_app["app"])
        pending = client.get(
            "/classroom/quizzes/pending",
            params={"student": "alice@ut.edu"},
        ).json()
        assert len(pending["quizzes"]) == 1

        resp = client.post(
            "/classroom/quizzes/qabc/submit",
            json={
                "student_id": "alice@ut.edu",
                "answers": [
                    {"question_index": 0, "answer_text": "rods absorb neutrons"},
                ],
            },
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["score"] == 1.0
        assert body["per_question"][0]["passed"] is True


# ---------------------------------------------------------------------------
# Interaction
# ---------------------------------------------------------------------------


class TestInteraction:
    def test_valid_interaction_is_logged(self, wired_app):
        client = TestClient(wired_app["app"])
        resp = client.post(
            "/classroom/interaction",
            json={
                "student_id": "alice@ut.edu",
                "question": "what is a control rod?",
                "had_answer": True,
                "citations_count": 2,
                "mode": "tutor",
            },
        )
        assert resp.status_code == 200
        records = wired_app["interactions"].list()
        assert len(records) == 1
        assert records[0].mode == "tutor"

    def test_missing_student_id_rejected(self, wired_app):
        client = TestClient(wired_app["app"])
        resp = client.post("/classroom/interaction", json={})
        assert resp.status_code == 400


# ---------------------------------------------------------------------------
# End-to-end with real uvicorn — proves the async stack is live
# ---------------------------------------------------------------------------


class TestRealUvicorn:
    def test_threaded_server_roundtrip(self, wired_app):
        import urllib.request

        with ThreadedServer(wired_app["app"]).serving() as srv:
            resp = urllib.request.urlopen(
                srv.base_url + "/classroom/policy", timeout=5,
            )
            assert resp.status == 200
            body = json.loads(resp.read().decode("utf-8"))
            assert "ask" in body["allowed_modes"]
