# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""End-to-end: student ask → interaction POST → instructor brief.

Proves the live-signals loop for CHALKE briefs. Without the push
from `ask` to `POST /classroom/interaction`, the instructor's brief
would stay empty; this test locks the wire together.
"""

from __future__ import annotations

import json
import threading
from http.server import HTTPServer

import pytest

from axiom.extensions.builtins.classroom.classroom_federation import (
    create_cohort,
)
from axiom.extensions.builtins.classroom.classroom_interaction import (
    ClassroomInteractionStore,
)
from axiom.extensions.builtins.classroom.classroom_materials import (
    ClassroomMaterialsStore,
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
from axiom.extensions.builtins.classroom.invite_token import (
    create_invite_token,
    encode_invite,
)
from axiom.vega.federation.identity import generate_identity


@pytest.fixture
def coordinator(tmp_path):
    coord = generate_identity(
        owner="prof@ut.edu", keys_dir=tmp_path / "coord-keys",
    )
    coord_dir = tmp_path / "coord"
    classroom_id = "NE101"

    cohort_store = FileCohortStore(coord_dir)
    cohort_store.save(
        create_cohort(classroom_id, coord.node_id),
        coordinator_url="http://placeholder/classroom/join",
    )
    invite_registry = FileInviteRegistry(coord_dir / "invites.json")

    materials = ClassroomMaterialsStore(
        coord_dir / "classrooms" / classroom_id
    )
    materials.add_text(
        "Control rods absorb neutrons to slow fission reactions.",
        filename="ch1.md", title="Chapter 1 — Control rods",
    )
    interactions = ClassroomInteractionStore(
        coord_dir / "classrooms" / classroom_id
    )

    handler_cls = make_coordinator_handler(
        coordinator_identity=coord,
        classroom_id=classroom_id,
        cohort_store=cohort_store,
        invite_registry=invite_registry,
        materials_store=materials,
        interaction_store=interactions,
    )
    server = HTTPServer(("127.0.0.1", 0), handler_cls)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    url = f"http://127.0.0.1:{server.server_port}/classroom/join"

    invite = create_invite_token(
        classroom_id=classroom_id,
        coordinator_id=coord.node_id,
        ttl_hours=24,
        coordinator_url=url,
    )
    invite_registry.register(invite)

    try:
        yield {
            "coord_dir": coord_dir,
            "classroom_id": classroom_id,
            "url": url,
            "encoded_invite": encode_invite(invite),
            "interaction_store": interactions,
        }
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.fixture
def student_home(tmp_path, monkeypatch):
    home = tmp_path / "student"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(
        "axiom.vega.federation.identity._DEFAULT_KEYS_DIR",
        home / "identity",
    )
    return home


# ---------------------------------------------------------------------------
# The loop: ask → POST → store
# ---------------------------------------------------------------------------


class TestAskPushesInteraction:
    def test_ask_logs_an_interaction_record_on_coordinator(
        self, coordinator, student_home, capsys,
    ):
        # 1. Student joins → materials sync → coordinator_url.txt written
        rc = main([
            "join", coordinator["encoded_invite"],
            "--student-id", "alice@example.org",
        ])
        assert rc == 0
        capsys.readouterr()

        # 2. Student asks → should POST one interaction record
        rc = main(["ask", coordinator["classroom_id"], "what is a control rod?"])
        assert rc == 0

        # 3. Coordinator's interaction store has the record
        records = coordinator["interaction_store"].list()
        assert len(records) == 1
        assert records[0].student_id == "alice@example.org"
        assert "control rod" in records[0].question.lower()
        assert records[0].had_answer is True
        assert records[0].citations_count >= 1

    def test_multiple_asks_accumulate(
        self, coordinator, student_home, capsys,
    ):
        main([
            "join", coordinator["encoded_invite"],
            "--student-id", "alice@example.org",
        ])
        capsys.readouterr()
        for q in ["control rod", "fission", "coolant"]:
            main(["ask", coordinator["classroom_id"], q])
            capsys.readouterr()

        records = coordinator["interaction_store"].list()
        assert len(records) == 3

    def test_ask_succeeds_even_if_coordinator_unreachable(
        self, coordinator, student_home, capsys, tmp_path,
    ):
        # Join first (while server is up).
        main([
            "join", coordinator["encoded_invite"],
            "--student-id", "alice@example.org",
        ])
        capsys.readouterr()

        # Point the sidecar at a nothing port so the interaction POST
        # fails transport. `ask` must still return 0.
        sidecar = (
            student_home / ".axi" / "classrooms"
            / coordinator["classroom_id"] / "coordinator_url.txt"
        )
        sidecar.write_text("http://127.0.0.1:1")  # connection refused

        rc = main(["ask", coordinator["classroom_id"], "what is a control rod?"])
        assert rc == 0


# ---------------------------------------------------------------------------
# Brief reads the live feed
# ---------------------------------------------------------------------------


class TestBriefReadsLiveSignals:
    def test_brief_surfaces_question_count_and_hot_topics(
        self, coordinator, capsys, monkeypatch, tmp_path,
    ):
        # Seed the coordinator's interaction store directly so we don't
        # depend on the wire path (which TestAskPushesInteraction
        # already exercises).
        from axiom.extensions.builtins.classroom.classroom_interaction import (
            InteractionRecord,
        )
        store = coordinator["interaction_store"]
        for q, sid in [
            ("What is a control rod?", "alice@ut.edu"),
            ("How do control rods work?", "alice@ut.edu"),
            ("What is a control rod made of?", "bob@ut.edu"),
            ("What is fission?", "bob@ut.edu"),
        ]:
            store.append(InteractionRecord(
                student_id=sid, question=q, had_answer=True,
                citations_count=1,
                timestamp="2026-04-23T10:00:00+00:00",
                classroom_id=coordinator["classroom_id"],
            ))

        # Instructor-side HOME points at the coord_dir's parent so
        # `_cmd_brief` reads from the same dir the fixture uses.
        instructor_home = coordinator["coord_dir"].parent
        monkeypatch.setenv("HOME", str(instructor_home))

        # `_cmd_brief` looks for `<HOME>/.axi/coordinator/classrooms/<id>/`.
        # The fixture wrote to `<tmp_path>/coord/classrooms/NE101/`. So we
        # need `<HOME>/.axi/coordinator` → `<tmp_path>/coord`. Symlink.
        (instructor_home / ".axi").mkdir(exist_ok=True)
        coord_link = instructor_home / ".axi" / "coordinator"
        if coord_link.exists() or coord_link.is_symlink():
            coord_link.unlink()
        coord_link.symlink_to(coordinator["coord_dir"])

        rc = main([
            "brief", coordinator["classroom_id"], "--instructor", "@prof:ut",
        ])
        assert rc == 0
        out = capsys.readouterr().out
        # Live signals showed up.
        assert "4" in out  # 4 questions total
        assert "control" in out.lower() or "rod" in out.lower()

    def test_brief_json_includes_live_signals(
        self, coordinator, capsys, monkeypatch,
    ):
        instructor_home = coordinator["coord_dir"].parent
        monkeypatch.setenv("HOME", str(instructor_home))
        (instructor_home / ".axi").mkdir(exist_ok=True)
        coord_link = instructor_home / ".axi" / "coordinator"
        if coord_link.exists() or coord_link.is_symlink():
            coord_link.unlink()
        coord_link.symlink_to(coordinator["coord_dir"])

        rc = main([
            "brief", coordinator["classroom_id"],
            "--instructor", "@prof:ut", "--format", "json",
        ])
        assert rc == 0
        data = json.loads(capsys.readouterr().out)
        assert "live_signals" in data
        assert "total_questions" in data["live_signals"]
        assert "hot_topics" in data["live_signals"]
