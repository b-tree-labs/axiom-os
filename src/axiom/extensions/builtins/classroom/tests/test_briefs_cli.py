# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tests for the brief CLI commands.

Instructor side:
    axi classroom briefs generate <class>
    axi classroom briefs list     <class>
    axi classroom briefs review   <class> <student_id>  [--note / --approve]

Student side:
    axi classroom me <class>   — fetches own latest APPROVED brief

Plus an end-to-end test that wires the whole loop through a real
HTTPServer to prove `me` reads over the wire.
"""

from __future__ import annotations

import json
import threading
from http.server import HTTPServer
from pathlib import Path

import pytest

from axiom.extensions.builtins.classroom.classroom_federation import (
    add_member,
    create_cohort,
)
from axiom.extensions.builtins.classroom.classroom_interaction import (
    ClassroomInteractionStore,
    InteractionRecord,
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
from axiom.extensions.builtins.classroom.student_briefs import BriefStore
from axiom.vega.federation.identity import generate_identity

# ---------------------------------------------------------------------------
# Instructor side
# ---------------------------------------------------------------------------


@pytest.fixture
def instructor_home(tmp_path, monkeypatch):
    home = tmp_path / "instructor"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(
        "axiom.vega.federation.identity._DEFAULT_KEYS_DIR",
        home / "identity",
    )
    identity = generate_identity(owner="prof@ut.edu", keys_dir=home / "identity")
    coord_dir = home / ".axi" / "coordinator"
    cohort_store = FileCohortStore(coord_dir)
    cohort = create_cohort("NE101", identity.node_id)
    cohort = add_member(cohort, "alice@ut.edu", "node_a", "tok_a")
    cohort = add_member(cohort, "bob@ut.edu", "node_b", "tok_b")
    cohort_store.save(cohort, coordinator_url="http://placeholder/classroom/join")

    # Seed some interactions so the briefs aren't empty.
    store = ClassroomInteractionStore(coord_dir / "classrooms" / "NE101")
    store.append(InteractionRecord(
        student_id="alice@ut.edu", question="What is a control rod?",
        had_answer=True, citations_count=1,
        timestamp="2026-04-23T10:00+00:00",
        classroom_id="NE101", mode="ask",
    ))
    store.append(InteractionRecord(
        student_id="bob@ut.edu", question="What is fission?",
        had_answer=True, citations_count=1,
        timestamp="2026-04-23T10:05+00:00",
        classroom_id="NE101", mode="tutor",
    ))
    return home


class TestBriefsGenerate:
    def test_generates_one_brief_per_cohort_member(
        self, instructor_home, capsys,
    ):
        rc = main(["briefs", "generate", "NE101", "--json"])
        assert rc == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["count"] == 2
        assert set(payload["generated_for"]) == {"alice@ut.edu", "bob@ut.edu"}

    def test_briefs_default_to_draft_status(
        self, instructor_home, capsys,
    ):
        main(["briefs", "generate", "NE101"])
        capsys.readouterr()
        store = BriefStore(
            instructor_home / ".axi" / "coordinator" / "classrooms" / "NE101"
        )
        brief = store.latest_for_student("alice@ut.edu")
        assert brief is not None
        assert brief.review_status == "draft"

    def test_generate_on_unknown_class_errors(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setenv("HOME", str(tmp_path / "empty"))
        rc = main(["briefs", "generate", "NE999"])
        assert rc == 1
        err = capsys.readouterr().err
        assert "NE999" in err


class TestBriefsList:
    def test_list_shows_drafts(self, instructor_home, capsys):
        main(["briefs", "generate", "NE101"])
        capsys.readouterr()
        rc = main(["briefs", "list", "NE101", "--json"])
        assert rc == 0
        payload = json.loads(capsys.readouterr().out)
        assert len(payload["briefs"]) == 2
        assert all(b["status"] == "draft" for b in payload["briefs"])

    def test_list_empty_before_generate(
        self, instructor_home, capsys,
    ):
        rc = main(["briefs", "list", "NE101"])
        assert rc == 0
        out = capsys.readouterr().out
        assert "no briefs" in out.lower() or "axi classroom briefs generate" in out.lower()


class TestBriefsReview:
    def test_approve_flips_status(self, instructor_home, capsys):
        main(["briefs", "generate", "NE101"])
        capsys.readouterr()
        rc = main([
            "briefs", "review", "NE101", "alice@ut.edu", "--approve",
        ])
        assert rc == 0
        # Verify by rereading.
        store = BriefStore(
            instructor_home / ".axi" / "coordinator" / "classrooms" / "NE101"
        )
        brief = store.latest_for_student("alice@ut.edu")
        assert brief.review_status == "approved"

    def test_note_is_attached(self, instructor_home, capsys):
        main(["briefs", "generate", "NE101"])
        capsys.readouterr()
        main([
            "briefs", "review", "NE101", "alice@ut.edu",
            "--note", "Keep at it — you're making progress.",
            "--approve",
        ])
        store = BriefStore(
            instructor_home / ".axi" / "coordinator" / "classrooms" / "NE101"
        )
        brief = store.latest_for_student("alice@ut.edu")
        assert "Keep at it" in brief.instructor_note

    def test_review_unknown_student_errors(self, instructor_home, capsys):
        main(["briefs", "generate", "NE101"])
        capsys.readouterr()
        rc = main(["briefs", "review", "NE101", "nobody@ut.edu"])
        assert rc == 1
        err = capsys.readouterr().err
        assert "no brief" in err.lower()


# ---------------------------------------------------------------------------
# Student side — end-to-end over real HTTP
# ---------------------------------------------------------------------------


@pytest.fixture
def live_classroom(tmp_path):
    """Instructor state + running coordinator + a released brief for alice."""
    coord = generate_identity(
        owner="prof@ut.edu", keys_dir=tmp_path / "coord-keys",
    )
    coord_dir = tmp_path / "coord"
    classroom_id = "NE101"

    cohort_store = FileCohortStore(coord_dir)
    cohort = create_cohort(classroom_id, coord.node_id)
    cohort = add_member(cohort, "alice@ut.edu", "alice_node", "tok_a")
    cohort_store.save(cohort, coordinator_url="http://placeholder/classroom/join")

    invite_registry = FileInviteRegistry(coord_dir / "invites.json")

    classroom_coord_dir = coord_dir / "classrooms" / classroom_id
    interactions = ClassroomInteractionStore(classroom_coord_dir)
    interactions.append(InteractionRecord(
        student_id="alice@ut.edu", question="What is a control rod?",
        had_answer=True, citations_count=1,
        timestamp="2026-04-23T10:00+00:00",
        classroom_id=classroom_id, mode="ask",
    ))

    # Generate + approve a brief so the student has something to fetch.
    from axiom.extensions.builtins.classroom.student_briefs import (
        BriefStore,
        generate_brief,
    )
    brief_store = BriefStore(classroom_coord_dir)
    brief = generate_brief(
        student_id="alice@ut.edu", classroom_id=classroom_id,
        interactions=interactions.list(), llm=None,
    )
    brief_store.save(brief)
    brief_store.approve("alice@ut.edu", brief.generated_at, note="")

    handler_cls = make_coordinator_handler(
        coordinator_identity=coord,
        classroom_id=classroom_id,
        cohort_store=cohort_store,
        invite_registry=invite_registry,
        brief_store=brief_store,
    )
    server = HTTPServer(("127.0.0.1", 0), handler_cls)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    try:
        yield {
            "coord_dir": coord_dir,
            "base_url": f"http://127.0.0.1:{server.server_port}",
            "classroom_id": classroom_id,
        }
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _seed_student_membership(
    *, student_home: Path, classroom_id: str, student_id: str, base_url: str,
):
    """Fake the artifacts `_cmd_me` reads: properly-signed membership
    record + coord URL sidecar. Uses a real signing key so
    MembershipStore.load's signature verification passes."""
    from axiom.extensions.builtins.classroom.classroom_coordinator import (
        sign_membership_manifest,
    )
    from axiom.extensions.builtins.classroom.classroom_federation import (
        ClassroomCohort,
        CohortMember,
    )
    from axiom.extensions.builtins.classroom.student_membership import (
        MembershipStore,
    )
    from axiom.vega.federation.identity import generate_identity

    class_dir = student_home / ".axi" / "classrooms" / classroom_id
    class_dir.mkdir(parents=True, exist_ok=True)
    # Sidecar contains the coordinator BASE URL (no /classroom/join
    # suffix) — that's what _push_interaction + _fetch_or_cache_my_brief
    # expect. The production flow writes this in _sync_and_index_materials.
    (class_dir / "coordinator_url.txt").write_text(base_url)

    coord_id = generate_identity(
        owner="prof@ut.edu", keys_dir=student_home / "coord-keys",
    )
    cohort = ClassroomCohort(
        classroom_id=classroom_id,
        coordinator_node=coord_id.node_id,
        members=[CohortMember(
            student_id=student_id,
            member_node="node",
            invite_token="tok",
            status="ACTIVE",
            joined_at="2026-04-23T10:00+00:00",
        )],
    )
    signed = sign_membership_manifest(
        identity=coord_id, cohort=cohort, student_id=student_id,
    )
    MembershipStore(base_dir=student_home / ".axi").save(
        manifest=signed,
        coordinator_public_key=coord_id.public_key,
    )


class TestStudentMe:
    def test_me_fetches_approved_brief_over_http(
        self, live_classroom, tmp_path, monkeypatch, capsys,
    ):
        student_home = tmp_path / "student"
        monkeypatch.setenv("HOME", str(student_home))
        _seed_student_membership(
            student_home=student_home,
            classroom_id="NE101",
            student_id="alice@ut.edu",
            base_url=live_classroom["base_url"],
        )
        rc = main(["me", "NE101", "--json"])
        assert rc == 0
        data = json.loads(capsys.readouterr().out)
        # Our seeded brief → JSON round-tripped back.
        assert data["student_id"] == "alice@ut.edu"
        assert data["classroom_id"] == "NE101"
        assert data["review_status"] == "approved"

    def test_me_prints_friendly_message_when_no_brief(
        self, tmp_path, monkeypatch, capsys,
    ):
        home = tmp_path / "student"
        class_dir = home / ".axi" / "classrooms" / "NE101"
        class_dir.mkdir(parents=True)
        monkeypatch.setenv("HOME", str(home))
        rc = main(["me", "NE101"])
        assert rc == 0
        out = capsys.readouterr().out
        assert (
            "no brief" in out.lower()
            or "no brief released yet" in out.lower()
        )

    def test_me_errors_when_not_a_member(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setenv("HOME", str(tmp_path / "empty"))
        rc = main(["me", "NE101"])
        assert rc == 1
        err = capsys.readouterr().err
        assert "join" in err.lower()

    def test_draft_brief_is_invisible_to_student(
        self, tmp_path, monkeypatch, capsys,
    ):
        """Only approved briefs cross the wire — drafts stay with the
        instructor until they explicitly approve."""
        coord = generate_identity(
            owner="prof@ut.edu", keys_dir=tmp_path / "coord-keys",
        )
        coord_dir = tmp_path / "coord"
        cohort_store = FileCohortStore(coord_dir)
        cohort = create_cohort("NE101", coord.node_id)
        cohort = add_member(cohort, "alice@ut.edu", "n", "t")
        cohort_store.save(
            cohort, coordinator_url="http://placeholder/classroom/join",
        )
        invite_registry = FileInviteRegistry(coord_dir / "invites.json")
        classroom_coord_dir = coord_dir / "classrooms" / "NE101"
        from axiom.extensions.builtins.classroom.student_briefs import (
            BriefStore,
            StudentBrief,
        )
        brief_store = BriefStore(classroom_coord_dir)
        # Save a DRAFT only — never approved.
        brief_store.save(StudentBrief(
            student_id="alice@ut.edu", classroom_id="NE101",
            period_start="", period_end="",
            generated_at="2026-04-23T10:00+00:00", sections={"x": 1},
        ))

        handler_cls = make_coordinator_handler(
            coordinator_identity=coord, classroom_id="NE101",
            cohort_store=cohort_store, invite_registry=invite_registry,
            brief_store=brief_store,
        )
        server = HTTPServer(("127.0.0.1", 0), handler_cls)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            student_home = tmp_path / "student"
            monkeypatch.setenv("HOME", str(student_home))
            _seed_student_membership(
                student_home=student_home, classroom_id="NE101",
                student_id="alice@ut.edu",
                base_url=f"http://127.0.0.1:{server.server_port}",
            )
            rc = main(["me", "NE101"])
            assert rc == 0
            out = capsys.readouterr().out
            assert "no brief" in out.lower()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
