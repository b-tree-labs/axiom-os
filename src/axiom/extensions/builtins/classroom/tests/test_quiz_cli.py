# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""End-to-end tests for the quiz CLI: instructor broadcasts, student
takes, both sides read results."""

from __future__ import annotations

import io
import json
import sys
import threading
from http.server import HTTPServer
from pathlib import Path

import pytest

from axiom.extensions.builtins.classroom.broadcast_quizzes import QuizStore
from axiom.extensions.builtins.classroom.classroom_coordinator import (
    sign_membership_manifest,
)
from axiom.extensions.builtins.classroom.classroom_federation import (
    add_member,
    create_cohort,
)
from axiom.extensions.builtins.classroom.cli import main
from axiom.extensions.builtins.classroom.coordinator_cohort_store import (
    FileCohortStore,
)
from axiom.extensions.builtins.classroom.coordinator_invite_registry import (
    FileInviteRegistry,
)
from axiom.extensions.builtins.classroom.coordinator_server import (
    make_coordinator_handler,
)
from axiom.extensions.builtins.classroom.student_membership import (
    MembershipStore,
)
from axiom.vega.federation.identity import generate_identity


@pytest.fixture
def live_coord(tmp_path):
    coord = generate_identity(owner="prof@ut.edu", keys_dir=tmp_path / "coord-keys")
    coord_dir = tmp_path / "coord"
    classroom_id = "NE101"

    cohort = create_cohort(classroom_id, coord.node_id)
    cohort = add_member(cohort, "alice@ut.edu", "node_a", "tok_a")
    cohort_store = FileCohortStore(coord_dir)
    cohort_store.save(cohort, coordinator_url="http://placeholder/classroom/join")
    invite_registry = FileInviteRegistry(coord_dir / "invites.json")

    quiz_store = QuizStore(coord_dir / "classrooms" / classroom_id)

    handler_cls = make_coordinator_handler(
        coordinator_identity=coord,
        classroom_id=classroom_id,
        cohort_store=cohort_store,
        invite_registry=invite_registry,
        quiz_store=quiz_store,
    )
    server = HTTPServer(("127.0.0.1", 0), handler_cls)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield {
            "coord": coord,
            "coord_dir": coord_dir,
            "cohort": cohort,
            "classroom_id": classroom_id,
            "base_url": f"http://127.0.0.1:{server.server_port}",
            "quiz_store": quiz_store,
        }
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _as_instructor(coord_dir: Path, monkeypatch) -> None:
    """Set HOME to a tmpdir containing a symlink to the coord dir so
    the role detector finds it as an instructor machine."""
    home = coord_dir.parent / "instructor-home"
    home.mkdir(exist_ok=True)
    (home / ".axi").mkdir(exist_ok=True)
    link = home / ".axi" / "coordinator"
    if link.exists() or link.is_symlink():
        link.unlink()
    link.symlink_to(coord_dir)
    monkeypatch.setenv("HOME", str(home))


def _as_student(
    tmp_path: Path, live_coord: dict, monkeypatch,
    student_id: str = "alice@ut.edu",
) -> Path:
    home = tmp_path / "student"
    home.mkdir(exist_ok=True)
    class_dir = home / ".axi" / "classrooms" / live_coord["classroom_id"]
    class_dir.mkdir(parents=True, exist_ok=True)
    (class_dir / "coordinator_url.txt").write_text(live_coord["base_url"])

    signed = sign_membership_manifest(
        identity=live_coord["coord"],
        cohort=live_coord["cohort"],
        student_id=student_id,
    )
    MembershipStore(base_dir=home / ".axi").save(
        manifest=signed,
        coordinator_public_key=live_coord["coord"].public_key,
    )
    monkeypatch.setenv("HOME", str(home))
    return home


# ---------------------------------------------------------------------------
# Instructor broadcast
# ---------------------------------------------------------------------------


class TestInstructorBroadcast:
    def test_broadcast_creates_quiz_from_shipped_bank(
        self, live_coord, monkeypatch, capsys,
    ):
        _as_instructor(live_coord["coord_dir"], monkeypatch)
        rc = main([
            "quiz", "broadcast", "NE101",
            "--bank-preset", "ne101-core",
            "--questions", "2",
            "--topic", "reactor basics",
            "--json",
        ])
        assert rc == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["classroom_id"] == "NE101"
        assert payload["question_count"] == 2
        assert payload["topic"] == "reactor basics"
        quizzes = live_coord["quiz_store"].list_all()
        assert len(quizzes) == 1

    def test_broadcast_category_filter(
        self, live_coord, monkeypatch, capsys,
    ):
        _as_instructor(live_coord["coord_dir"], monkeypatch)
        rc = main([
            "quiz", "broadcast", "NE101",
            "--bank-preset", "ne101-core",
            "--category", "reactor_core",
            "--questions", "2",
            "--json",
        ])
        assert rc == 0
        quizzes = live_coord["quiz_store"].list_all()
        assert len(quizzes) == 1
        for q in quizzes[0].questions:
            assert q.category == "reactor_core"

    def test_broadcast_unknown_category_errors(
        self, live_coord, monkeypatch, capsys,
    ):
        _as_instructor(live_coord["coord_dir"], monkeypatch)
        rc = main([
            "quiz", "broadcast", "NE101",
            "--bank-preset", "ne101-core",
            "--category", "no-such-category",
        ])
        assert rc == 1
        err = capsys.readouterr().err
        assert "no-such-category" in err.lower() or "no questions" in err.lower()

    def test_broadcast_rejects_non_instructor(
        self, tmp_path, monkeypatch, capsys,
    ):
        monkeypatch.setenv("HOME", str(tmp_path / "nowhere"))
        rc = main(["quiz", "broadcast", "NE101"])
        assert rc == 1
        err = capsys.readouterr().err
        assert "coordinator" in err.lower() or "isn't on this machine" in err.lower()


# ---------------------------------------------------------------------------
# Student pending + take round-trip
# ---------------------------------------------------------------------------


class TestStudentTakesQuiz:
    def test_pending_lists_broadcast(
        self, live_coord, tmp_path, monkeypatch, capsys,
    ):
        # Instructor broadcasts.
        _as_instructor(live_coord["coord_dir"], monkeypatch)
        main([
            "quiz", "broadcast", "NE101",
            "--bank-preset", "ne101-core", "--questions", "2", "--json",
        ])
        capsys.readouterr()

        # Student pulls pending.
        _as_student(tmp_path, live_coord, monkeypatch)
        rc = main(["quiz", "pending", "NE101", "--json"])
        assert rc == 0
        data = json.loads(capsys.readouterr().out)
        assert len(data["quizzes"]) == 1
        assert len(data["quizzes"][0]["questions"]) == 2

    def test_student_can_take_and_gets_score(
        self, live_coord, tmp_path, monkeypatch, capsys,
    ):
        _as_instructor(live_coord["coord_dir"], monkeypatch)
        main([
            "quiz", "broadcast", "NE101",
            "--bank-preset", "ne101-core", "--questions", "1", "--json",
        ])
        quiz_id = json.loads(capsys.readouterr().out)["quiz_id"]

        # Stand in for interactive input by feeding a stdin with one
        # answer. Fill with keywords that should pass the first NE101
        # question's scorer (the bank always has at least one keyword).
        _as_student(tmp_path, live_coord, monkeypatch)

        # Build an answer likely to hit — just stuff every ne101 core
        # keyword we know into it.
        likely_answer = (
            "heavy nuclei split releasing neutrons; control rods absorb "
            "them in boron or cadmium; fuel assemblies lattice; primary "
            "coolant loop transfers heat to steam; criticality chain "
            "self-sustain; decay heat after shutdown; defense barriers; "
            "scram rapid shutdown; domestic facilities; NRC license; "
            "moderator slow; light nuclei combine; pressure water PWR"
        )
        fake_stdin = io.StringIO(f"{likely_answer}\n")
        monkeypatch.setattr(sys, "stdin", fake_stdin)

        rc = main(["quiz", "take", "NE101", quiz_id, "--json"])
        assert rc == 0
        result = json.loads(capsys.readouterr().out)
        assert result["quiz_id"] == quiz_id
        assert "score" in result
        # Submission is durable on the coordinator side.
        assert live_coord["quiz_store"].has_submitted(quiz_id, "alice@ut.edu")

    def test_taken_quiz_disappears_from_pending(
        self, live_coord, tmp_path, monkeypatch, capsys,
    ):
        _as_instructor(live_coord["coord_dir"], monkeypatch)
        main([
            "quiz", "broadcast", "NE101",
            "--bank-preset", "ne101-core", "--questions", "1", "--json",
        ])
        quiz_id = json.loads(capsys.readouterr().out)["quiz_id"]

        _as_student(tmp_path, live_coord, monkeypatch)
        fake_stdin = io.StringIO("some answer\n")
        monkeypatch.setattr(sys, "stdin", fake_stdin)
        main(["quiz", "take", "NE101", quiz_id, "--json"])
        capsys.readouterr()

        rc = main(["quiz", "pending", "NE101", "--json"])
        assert rc == 0
        data = json.loads(capsys.readouterr().out)
        assert data["quizzes"] == []


# ---------------------------------------------------------------------------
# Instructor results
# ---------------------------------------------------------------------------


class TestInstructorResults:
    def test_results_summarize_scored_submissions(
        self, live_coord, tmp_path, monkeypatch, capsys,
    ):
        _as_instructor(live_coord["coord_dir"], monkeypatch)
        main([
            "quiz", "broadcast", "NE101",
            "--bank-preset", "ne101-core", "--questions", "1", "--json",
        ])
        quiz_id = json.loads(capsys.readouterr().out)["quiz_id"]

        # Alice takes the quiz.
        _as_student(tmp_path, live_coord, monkeypatch)
        fake_stdin = io.StringIO("heavy nuclei neutron\n")
        monkeypatch.setattr(sys, "stdin", fake_stdin)
        main(["quiz", "take", "NE101", quiz_id, "--json"])
        capsys.readouterr()

        # Instructor reviews.
        _as_instructor(live_coord["coord_dir"], monkeypatch)
        rc = main(["quiz", "results", "NE101", quiz_id, "--json"])
        assert rc == 0
        data = json.loads(capsys.readouterr().out)
        assert data["submission_count"] == 1
        assert data["submissions"][0]["student_id"] == "alice@ut.edu"
