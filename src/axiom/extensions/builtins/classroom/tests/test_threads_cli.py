# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""End-to-end tests for the thread CLI: student ↔ instructor loop
over a real HTTPServer.
"""

from __future__ import annotations

import json
import threading
from http.server import HTTPServer
from pathlib import Path

import pytest

from axiom.extensions.builtins.classroom.classroom_coordinator import (
    sign_membership_manifest,
)
from axiom.extensions.builtins.classroom.classroom_federation import (
    add_member,
    create_cohort,
)
from axiom.extensions.builtins.classroom.classroom_threads import ThreadStore
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

    thread_store = ThreadStore(coord_dir / "classrooms" / classroom_id)

    handler_cls = make_coordinator_handler(
        coordinator_identity=coord,
        classroom_id=classroom_id,
        cohort_store=cohort_store,
        invite_registry=invite_registry,
        thread_store=thread_store,
    )
    server = HTTPServer(("127.0.0.1", 0), handler_cls)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield {
            "coord": coord,
            "coord_dir": coord_dir,
            "classroom_id": classroom_id,
            "cohort": cohort,
            "base_url": f"http://127.0.0.1:{server.server_port}",
            "thread_store": thread_store,
        }
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _seed_student(
    *, student_home: Path, classroom_id: str, student_id: str,
    coord, cohort, base_url: str,
):
    """Minimum state for `_cmd_*` to recognize a student machine."""
    class_dir = student_home / ".axi" / "classrooms" / classroom_id
    class_dir.mkdir(parents=True, exist_ok=True)
    (class_dir / "coordinator_url.txt").write_text(base_url)
    signed = sign_membership_manifest(
        identity=coord, cohort=cohort, student_id=student_id,
    )
    MembershipStore(base_dir=student_home / ".axi").save(
        manifest=signed, coordinator_public_key=coord.public_key,
    )


# ---------------------------------------------------------------------------
# Student → instructor loop
# ---------------------------------------------------------------------------


class TestStudentOpensThread:
    def test_ask_instructor_creates_thread(
        self, live_coord, tmp_path, monkeypatch, capsys,
    ):
        home = tmp_path / "student"
        monkeypatch.setenv("HOME", str(home))
        _seed_student(
            student_home=home,
            classroom_id=live_coord["classroom_id"],
            student_id="alice@ut.edu",
            coord=live_coord["coord"],
            cohort=live_coord["cohort"],
            base_url=live_coord["base_url"],
        )
        rc = main([
            "ask-instructor", "NE101", "I'm stuck on control rods",
            "--json",
        ])
        assert rc == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["student_id"] == "alice@ut.edu"
        assert payload["status"] == "open"
        assert payload["opened_by"] == "student"
        assert len(payload["messages"]) == 1
        assert "control rods" in payload["messages"][0]["text"]

        # Server actually stored it.
        threads = live_coord["thread_store"].list_all()
        assert len(threads) == 1
        assert threads[0].student_id == "alice@ut.edu"


# ---------------------------------------------------------------------------
# Instructor side — ask-student + reply locally
# ---------------------------------------------------------------------------


class TestInstructorSide:
    def test_instructor_ask_student_creates_thread(
        self, live_coord, monkeypatch, capsys,
    ):
        # HOME points at the coord parent — instructor machine.
        monkeypatch.setenv("HOME", str(live_coord["coord_dir"].parent))
        (live_coord["coord_dir"].parent / ".axi").mkdir(exist_ok=True)
        # Symlink the fixture's coord_dir into HOME/.axi/coordinator so
        # the role detector finds it there.
        coord_link = live_coord["coord_dir"].parent / ".axi" / "coordinator"
        if coord_link.exists() or coord_link.is_symlink():
            coord_link.unlink()
        coord_link.symlink_to(live_coord["coord_dir"])

        rc = main([
            "ask-student", "NE101", "alice@ut.edu",
            "What did you think about chapter 2?",
            "--json",
        ])
        assert rc == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["opened_by"] == "instructor"
        assert payload["student_id"] == "alice@ut.edu"
        threads = live_coord["thread_store"].list_all()
        assert any(
            t.opened_by == "instructor"
            and t.student_id == "alice@ut.edu"
            for t in threads
        )

    def test_instructor_threads_lists_all(
        self, live_coord, monkeypatch, capsys,
    ):
        monkeypatch.setenv("HOME", str(live_coord["coord_dir"].parent))
        (live_coord["coord_dir"].parent / ".axi").mkdir(exist_ok=True)
        coord_link = live_coord["coord_dir"].parent / ".axi" / "coordinator"
        if coord_link.exists() or coord_link.is_symlink():
            coord_link.unlink()
        coord_link.symlink_to(live_coord["coord_dir"])

        # Create two threads through local store.
        main(["ask-student", "NE101", "alice@ut.edu", "q1"])
        capsys.readouterr()
        main(["ask-student", "NE101", "bob@ut.edu", "q2"])
        capsys.readouterr()

        rc = main(["threads", "NE101", "--json"])
        assert rc == 0
        payload = json.loads(capsys.readouterr().out)
        assert len(payload["threads"]) == 2


# ---------------------------------------------------------------------------
# Round trip — student opens, instructor replies, student sees the reply
# ---------------------------------------------------------------------------


class TestRoundTrip:
    def test_full_student_instructor_conversation(
        self, live_coord, tmp_path, monkeypatch, capsys,
    ):
        # --- student opens ---
        student_home = tmp_path / "student"
        monkeypatch.setenv("HOME", str(student_home))
        _seed_student(
            student_home=student_home,
            classroom_id=live_coord["classroom_id"],
            student_id="alice@ut.edu",
            coord=live_coord["coord"],
            cohort=live_coord["cohort"],
            base_url=live_coord["base_url"],
        )
        rc = main(["ask-instructor", "NE101", "Help on control rods?", "--json"])
        assert rc == 0
        thread_id = json.loads(capsys.readouterr().out)["thread_id"]

        # --- switch to instructor HOME to reply ---
        instructor_home = tmp_path / "instructor"
        instructor_home.mkdir(exist_ok=True)
        monkeypatch.setenv("HOME", str(instructor_home))
        (instructor_home / ".axi").mkdir(exist_ok=True)
        coord_link = instructor_home / ".axi" / "coordinator"
        if coord_link.exists() or coord_link.is_symlink():
            coord_link.unlink()
        coord_link.symlink_to(live_coord["coord_dir"])

        rc = main([
            "reply", "NE101", thread_id,
            "What have you tried so far?", "--json",
        ])
        assert rc == 0
        reply_payload = json.loads(capsys.readouterr().out)
        assert reply_payload["status"] == "answered"
        assert reply_payload["message_count"] == 2

        # --- back to student — should see both messages ---
        monkeypatch.setenv("HOME", str(student_home))
        rc = main(["threads", "NE101", "--json"])
        assert rc == 0
        threads = json.loads(capsys.readouterr().out)["threads"]
        assert len(threads) == 1
        assert threads[0]["status"] == "answered"
        assert len(threads[0]["messages"]) == 2


# ---------------------------------------------------------------------------
# Role-detection error paths
# ---------------------------------------------------------------------------


class TestRoleErrors:
    def test_ask_instructor_without_membership_errors(
        self, tmp_path, monkeypatch, capsys,
    ):
        monkeypatch.setenv("HOME", str(tmp_path / "empty"))
        rc = main(["ask-instructor", "NE101", "hello"])
        assert rc == 1
        err = capsys.readouterr().err
        assert "join" in err.lower()

    def test_ask_student_without_being_instructor_errors(
        self, tmp_path, monkeypatch, capsys,
    ):
        monkeypatch.setenv("HOME", str(tmp_path / "empty"))
        rc = main(["ask-student", "NE101", "alice@ut.edu", "msg"])
        assert rc == 1
        err = capsys.readouterr().err
        assert "NE101" in err
