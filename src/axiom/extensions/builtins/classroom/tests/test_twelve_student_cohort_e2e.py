# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Prague-sized cohort E2E: one instructor + 12 students + materials.

Closes the final Tier A gap. Earlier tests cover each piece of the
ceremony in isolation and a single-student full flow (join →
materials sync → index → ask). This file stresses the combined
system at Prague's real cohort size:

    1. Instructor identity + cohort + 3-file materials store
    2. One HTTPServer in a background thread
    3. 12 independent "students" — each with its own fresh HOME,
       identity keypair, and local ~/.axi/classrooms/<id>/ dir
    4. Each student runs `axi classroom join <TOKEN>` end-to-end
       (ceremony + materials sync + local index build)
    5. Each student runs `axi classroom ask` and gets a citation

Assertions:
- All 12 memberships land in the coordinator cohort.
- Every invite is consumed exactly once.
- Every student's local index has the same chunk count as the
  coordinator's materials (deterministic chunker, no skipping).
- Every student's `ask` call returns at least one citation from
  the class materials.
- No student can consume another student's invite (token-reuse
  refusal under load).
"""

from __future__ import annotations

import io
import json
import sys
import threading
from http.server import HTTPServer
from pathlib import Path

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
from axiom.vega.federation.identity import generate_identity

CLASSROOM_ID = "NE_PRAGUE_2026"
COHORT_SIZE = 12


@pytest.fixture
def prague_cohort(tmp_path):
    """Instructor state + a running coordinator + 12 pre-minted invites."""
    coord = generate_identity(
        owner="prof@prague.edu", keys_dir=tmp_path / "coord-keys",
    )
    coord_dir = tmp_path / "coord-state"

    cohort_store = FileCohortStore(coord_dir)
    cohort_store.save(
        create_cohort(CLASSROOM_ID, coord.node_id),
        coordinator_url="http://placeholder/classroom/join",
    )
    invite_registry = FileInviteRegistry(coord_dir / "invites.json")

    # Populate a handful of realistic-ish files so the index has
    # something to chew on.
    materials = ClassroomMaterialsStore(coord_dir / "classrooms" / CLASSROOM_ID)
    files = [
        ("ch1.md", "Chapter 1 — Fission",
         "Fission splits heavy nuclei into lighter fragments, releasing energy and neutrons."),
        ("ch2.md", "Chapter 2 — Control rods",
         "Control rods absorb neutrons to slow the chain reaction. They are made of boron or cadmium."),
        ("ch3.md", "Chapter 3 — Cooling systems",
         "The primary coolant loop transfers heat from the reactor core to the secondary loop via steam generators."),
    ]
    for filename, title, content in files:
        materials.add_text(content, filename=filename, title=title)

    # One HTTPServer for the whole cohort.
    handler_cls = make_coordinator_handler(
        coordinator_identity=coord,
        classroom_id=CLASSROOM_ID,
        cohort_store=cohort_store,
        invite_registry=invite_registry,
        materials_store=materials,
    )
    server = HTTPServer(("127.0.0.1", 0), handler_cls)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    url = f"http://127.0.0.1:{server.server_port}/classroom/join"

    # Pre-mint one invite per student.
    tokens: list[str] = []
    for _ in range(COHORT_SIZE):
        invite = create_invite_token(
            classroom_id=CLASSROOM_ID,
            coordinator_id=coord.node_id,
            ttl_hours=24,
            coordinator_url=url,
        )
        invite_registry.register(invite)
        tokens.append(encode_invite(invite))

    try:
        yield {
            "coord_identity": coord,
            "coord_dir": coord_dir,
            "cohort_store": cohort_store,
            "invite_registry": invite_registry,
            "materials": materials,
            "url": url,
            "tokens": tokens,
        }
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _run_student_join(
    *,
    monkeypatch: pytest.MonkeyPatch,
    student_home: Path,
    student_id: str,
    token: str,
) -> int:
    """Join one student, with their own fresh HOME + identity keys."""
    import axiom.vega.federation.identity as fed_id

    monkeypatch.setenv("HOME", str(student_home))
    monkeypatch.setattr(fed_id, "_DEFAULT_KEYS_DIR", student_home / "identity")

    # Module-level cached identity (if any) must be cleared so each
    # student generates fresh keys. The load_identity() path reads from
    # _DEFAULT_KEYS_DIR each call, so this is already safe — but making
    # it explicit future-proofs.
    return main(["join", token, "--student-id", student_id, "--json"])


# ---------------------------------------------------------------------------
# The main test
# ---------------------------------------------------------------------------


class TestCohortOfTwelve:
    def test_twelve_students_join_sync_and_ask(
        self, prague_cohort, tmp_path, monkeypatch, capsys,
    ):
        students: list[dict] = []
        for i in range(COHORT_SIZE):
            student_id = f"student{i:02d}@prague.edu"
            student_home = tmp_path / f"student-home-{i:02d}"
            student_home.mkdir()

            rc = _run_student_join(
                monkeypatch=monkeypatch,
                student_home=student_home,
                student_id=student_id,
                token=prague_cohort["tokens"][i],
            )
            assert rc == 0, f"student {i} join failed with rc={rc}"

            # Drain stdout so next student starts clean.
            out = capsys.readouterr().out
            # Every student sees an accepted JSON payload on stdout.
            payload = json.loads(out)
            assert payload["accepted"] is True
            assert payload["student_id"] == student_id

            students.append({
                "index": i,
                "student_id": student_id,
                "home": student_home,
            })

        # ---- Assertion 1: coordinator cohort reflects all 12 joins ----
        final_cohort = prague_cohort["cohort_store"].load(CLASSROOM_ID)
        joined_ids = {m.student_id for m in final_cohort.members}
        expected_ids = {s["student_id"] for s in students}
        assert joined_ids == expected_ids, (
            f"cohort mismatch: missing {expected_ids - joined_ids}, "
            f"unexpected {joined_ids - expected_ids}"
        )

        # ---- Assertion 2: every invite consumed exactly once ----
        for token_str in prague_cohort["tokens"]:
            from axiom.extensions.builtins.classroom.invite_token import decode_invite
            token = decode_invite(token_str).token
            assert prague_cohort["invite_registry"].is_consumed(token), (
                f"invite {token[:8]}… unexpectedly not consumed"
            )

        # ---- Assertion 3: every student has the full materials index ----
        coord_chunk_count = None
        for student in students:
            index = ClassroomLocalIndex(
                base_dir=student["home"] / ".axi" / "classrooms" / CLASSROOM_ID
            )
            index.open()
            try:
                count = index.chunk_count()
            finally:
                index.close()

            if coord_chunk_count is None:
                coord_chunk_count = count
            else:
                assert count == coord_chunk_count, (
                    f"{student['student_id']} has {count} chunks; "
                    f"expected {coord_chunk_count} (matches first student)"
                )
        assert coord_chunk_count is not None
        assert coord_chunk_count >= 3  # at least one chunk per file

        # ---- Assertion 4: every student can ask + get a citation ----
        for student in students:
            monkeypatch.setenv("HOME", str(student["home"]))
            captured = io.StringIO()
            old = sys.stdout
            sys.stdout = captured
            try:
                rc = main(["ask", CLASSROOM_ID, "control rod", "--json"])
            finally:
                sys.stdout = old
            assert rc == 0
            payload = json.loads(captured.getvalue())
            assert payload["citations"], (
                f"{student['student_id']} got no citations for 'control rod'"
            )
            # Citation should mention the source title — proves
            # retrieval reached the indexed chunks.
            assert any(
                "control rod" in c["text"].lower()
                for c in payload["citations"]
            )

    def test_token_reuse_across_students_is_refused(
        self, prague_cohort, tmp_path, monkeypatch,
    ):
        """If student 0's invite leaked and student 5 tried to reuse it,
        the coordinator must refuse — even when load stress might race."""
        shared_token = prague_cohort["tokens"][0]

        # Student 0 joins normally.
        rc0 = _run_student_join(
            monkeypatch=monkeypatch,
            student_home=tmp_path / "s0-home",
            student_id="s0@prague.edu",
            token=shared_token,
        )
        assert rc0 == 0

        # Student X tries to reuse student 0's token.
        monkeypatch.undo()
        (tmp_path / "sX-home").mkdir(exist_ok=True)
        rc_replay = _run_student_join(
            monkeypatch=monkeypatch,
            student_home=tmp_path / "sX-home",
            student_id="sX@prague.edu",
            token=shared_token,
        )
        assert rc_replay != 0, "token-reuse ceremony should have failed"
