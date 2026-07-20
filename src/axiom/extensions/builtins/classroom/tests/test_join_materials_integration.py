# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""End-to-end: join ceremony auto-pulls + indexes course materials.

Phase 5 of the materials-flow tier. Glues Phases 1-4 together at the
`axi classroom join` command:

    1. Ceremony succeeds (existing Tier A flow)
    2. Student auto-calls `/classroom/materials/manifest`
    3. Verifies signature, downloads files, verifies per-file hashes
    4. Runs each file through `ClassroomLocalIndex.ingest`
    5. Narrates progress + final count to the student

Back-compat: if the coordinator is an older version without materials
endpoints (manifest 404s), the join still succeeds — sync is optional.
"""

from __future__ import annotations

import json
import threading
from http.server import HTTPServer

import pytest

from axiom.extensions.builtins.classroom.classroom_federation import (
    create_cohort,
)
from axiom.extensions.builtins.classroom.classroom_local_index import (
    ClassroomLocalIndex,
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
from axiom.extensions.builtins.classroom.materials_sync import (
    StudentMaterialsStore,
)
from axiom.vega.federation.identity import generate_identity


@pytest.fixture
def coordinator(tmp_path):
    coord = generate_identity(
        owner="prof@ut.edu", keys_dir=tmp_path / "coord-keys"
    )
    coord_dir = tmp_path / "coord-state"
    classroom_id = "NE101"

    cohort = create_cohort(classroom_id, coord.node_id)
    cohort_store = FileCohortStore(coord_dir)
    cohort_store.save(cohort, coordinator_url="http://placeholder/classroom/join")
    invite_registry = FileInviteRegistry(coord_dir / "invites.json")

    materials = ClassroomMaterialsStore(
        coord_dir / "classrooms" / classroom_id
    )
    materials.add_text(
        "Control rods absorb neutrons to slow fission.",
        filename="ch1.md", title="Chapter 1",
    )
    materials.add_text(
        "Fuel assemblies sit in a lattice cooled by water.",
        filename="ch2.md", title="Chapter 2",
    )

    handler_cls = make_coordinator_handler(
        coordinator_identity=coord,
        classroom_id=classroom_id,
        cohort_store=cohort_store,
        invite_registry=invite_registry,
        materials_store=materials,
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
            "coord_identity": coord,
            "coord_dir": coord_dir,
            "classroom_id": classroom_id,
            "url": url,
            "encoded_invite": encode_invite(invite),
        }
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.fixture
def home_tmp(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "student-home"))
    monkeypatch.setattr(
        "axiom.vega.federation.identity._DEFAULT_KEYS_DIR",
        tmp_path / "student-home" / "identity",
    )
    return tmp_path / "student-home"


# ---------------------------------------------------------------------------
# Happy path — join also syncs + indexes
# ---------------------------------------------------------------------------


class TestJoinAutoSyncs:
    def test_join_downloads_materials_to_student_disk(
        self, coordinator, home_tmp, capsys,
    ):
        rc = main([
            "join", coordinator["encoded_invite"],
            "--student-id", "alice@example.org",
        ])
        assert rc == 0

        student_store = StudentMaterialsStore(
            home_tmp / ".axi" / "classrooms" / coordinator["classroom_id"]
        )
        entries = student_store.list_entries()
        assert len(entries) == 2

    def test_join_indexes_materials_for_search(
        self, coordinator, home_tmp, capsys,
    ):
        rc = main([
            "join", coordinator["encoded_invite"],
            "--student-id", "alice@example.org",
        ])
        assert rc == 0

        index = ClassroomLocalIndex(
            base_dir=home_tmp / ".axi" / "classrooms" / coordinator["classroom_id"]
        )
        index.open()
        try:
            hits = index.search("control rod", k=5)
        finally:
            index.close()
        assert hits
        assert any("control rod" in h.text.lower() for h in hits)

    def test_join_narrates_materials_sync(
        self, coordinator, home_tmp, capsys,
    ):
        rc = main([
            "join", coordinator["encoded_invite"],
            "--student-id", "alice@example.org",
        ])
        assert rc == 0
        captured = capsys.readouterr()
        combined = captured.out + captured.err
        # Student-friendly narration — don't say "manifest" or "chunks".
        lower = combined.lower()
        assert "materials" in lower or "references" in lower
        # No jargon leaks.
        for forbidden in ("manifest", "sqlite", "chunks", "embedding"):
            assert forbidden not in lower, f"narration leaked {forbidden!r}"


# ---------------------------------------------------------------------------
# JSON mode — narration on stderr, JSON stays parseable on stdout
# ---------------------------------------------------------------------------


class TestJsonModeKeepsStdoutParseable:
    def test_json_mode_does_not_bleed_narration_into_stdout(
        self, coordinator, home_tmp, capsys,
    ):
        rc = main([
            "join", coordinator["encoded_invite"],
            "--student-id", "alice@example.org",
            "--json",
        ])
        assert rc == 0
        captured = capsys.readouterr()
        # stdout parses cleanly as JSON.
        data = json.loads(captured.out)
        assert data["accepted"] is True


# ---------------------------------------------------------------------------
# Back-compat — older coordinator without materials endpoint still succeeds
# ---------------------------------------------------------------------------


class TestLegacyCoordinatorCompat:
    def test_join_succeeds_when_coordinator_has_no_materials(
        self, home_tmp, tmp_path, capsys,
    ):
        """If `make_coordinator_handler` is called without materials_store
        (legacy pre-materials coordinators), the join succeeds and the
        student just doesn't get materials — no crash, no confusing
        error."""
        coord = generate_identity(
            owner="prof@ut.edu", keys_dir=tmp_path / "coord-keys"
        )
        coord_dir = tmp_path / "coord-state"
        classroom_id = "OLD_COORD_CLASS"

        cohort_store = FileCohortStore(coord_dir)
        cohort_store.save(
            create_cohort(classroom_id, coord.node_id),
            coordinator_url="http://placeholder/classroom/join",
        )
        invite_registry = FileInviteRegistry(coord_dir / "invites.json")

        handler_cls = make_coordinator_handler(
            coordinator_identity=coord,
            classroom_id=classroom_id,
            cohort_store=cohort_store,
            invite_registry=invite_registry,
            # materials_store deliberately omitted
        )
        server = HTTPServer(("127.0.0.1", 0), handler_cls)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            url = f"http://127.0.0.1:{server.server_port}/classroom/join"
            invite = create_invite_token(
                classroom_id=classroom_id,
                coordinator_id=coord.node_id,
                ttl_hours=24,
                coordinator_url=url,
            )
            invite_registry.register(invite)

            rc = main([
                "join", encode_invite(invite),
                "--student-id", "alice@ut.edu",
            ])
            assert rc == 0

            # No student-side materials index was created.
            student_store = StudentMaterialsStore(
                home_tmp / ".axi" / "classrooms" / classroom_id
            )
            assert student_store.list_entries() == []
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
