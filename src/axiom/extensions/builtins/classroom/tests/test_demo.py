# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tests for ``axi classroom demo`` — FW-1 P1 (seeded demo classroom).

The demo delivers skeptic-evaluation-in-60s per prd-classroom §2.5
(enter through the end). Key invariants:

- Seeded classroom is fully-populated: corpus indexed, prompt set,
  rails + assessments authored, roster synced, RAG policy chosen.
- Running ``demo`` twice is idempotent (same content, same artifact
  data).
- ``demo --reset`` wipes the demo artifacts before re-seeding so a
  user who's poked at the demo can return to a known state.
- ``prep from-demo <new_id>`` clones the demo course into a fresh
  editable course and does NOT touch the demo artifacts themselves.

Runtime paths are isolated via AXIOM_RUNTIME_ROOT in tests.
"""

from __future__ import annotations

import pytest

from axiom.extensions.builtins.classroom.demo import (
    DEMO_CLASSROOM_ID,
    DEMO_COURSE_ID,
    reset_demo,
    seed_demo,
)
from axiom.extensions.builtins.classroom.operational_store import (
    load_classroom_data,
    load_course_data,
)


@pytest.fixture(autouse=True)
def _isolated_runtime(tmp_path, monkeypatch):
    """Every test gets its own runtime root so demo state can't leak."""
    monkeypatch.setenv("AXIOM_RUNTIME_ROOT", str(tmp_path))
    # Reset the module-level registry cache so tests can't contaminate each
    # other via the singleton in operational_store.
    import axiom.extensions.builtins.classroom.operational_store as store

    store._registry = None
    yield
    store._registry = None


class TestSeedDemo:
    def test_creates_course_artifact(self):
        seed_demo()
        data = load_course_data(DEMO_COURSE_ID)
        assert data is not None
        assert data["id"] == DEMO_COURSE_ID
        assert "Classical Mechanics" in data["title"]

    def test_creates_classroom_artifact(self):
        seed_demo()
        data = load_classroom_data(DEMO_CLASSROOM_ID)
        assert data is not None
        assert data["id"] == DEMO_CLASSROOM_ID
        assert data["course_id"] == DEMO_COURSE_ID

    def test_course_has_full_manifest(self):
        seed_demo()
        data = load_course_data(DEMO_COURSE_ID)
        manifest = data["manifest"]
        assert manifest is not None
        assert manifest.get("title")
        # Learning objectives populated
        assert len(manifest.get("learning_objectives", [])) >= 3

    def test_course_has_system_prompt(self):
        seed_demo()
        data = load_course_data(DEMO_COURSE_ID)
        assert data["system_prompt"]
        assert len(data["system_prompt"]) > 50

    def test_course_has_corpus_indexed(self):
        seed_demo()
        data = load_course_data(DEMO_COURSE_ID)
        assert data["corpus_doc_count"] >= 10

    def test_course_has_assessments(self):
        """Default checkpoints: baseline + midpoint (per course_checkpoints memory)."""
        seed_demo()
        data = load_course_data(DEMO_COURSE_ID)
        assessments = data.get("assessments", [])
        assert len(assessments) >= 2
        ids = {a.get("id") for a in assessments}
        assert "baseline" in ids
        assert "midpoint" in ids

    def test_course_has_rails(self):
        seed_demo()
        data = load_course_data(DEMO_COURSE_ID)
        assert len(data.get("rails", [])) >= 1

    def test_classroom_has_roster(self):
        seed_demo()
        data = load_classroom_data(DEMO_CLASSROOM_ID)
        roster = data.get("lms_roster", [])
        assert len(roster) == 5
        # Matrix-style principals (@name:demo)
        for student in roster:
            assert student.get("principal", "").startswith("@")
            assert ":demo" in student.get("principal", "")

    def test_classroom_has_rag_policy(self):
        seed_demo()
        data = load_classroom_data(DEMO_CLASSROOM_ID)
        assert data.get("rag_policy_mode")


class TestIdempotence:
    def test_seeding_twice_yields_same_content(self):
        seed_demo()
        first_course = load_course_data(DEMO_COURSE_ID)
        first_classroom = load_classroom_data(DEMO_CLASSROOM_ID)

        seed_demo()
        second_course = load_course_data(DEMO_COURSE_ID)
        second_classroom = load_classroom_data(DEMO_CLASSROOM_ID)

        # The artifact registry versions on each write, but the *payload*
        # must be identical so re-seeding doesn't surprise the user.
        assert first_course["manifest"] == second_course["manifest"]
        assert first_course["system_prompt"] == second_course["system_prompt"]
        assert first_course["assessments"] == second_course["assessments"]
        assert first_course["rails"] == second_course["rails"]
        assert first_classroom["lms_roster"] == second_classroom["lms_roster"]


class TestReset:
    def test_reset_after_seed_yields_fresh_state(self):
        seed_demo()
        # Simulate the user mucking with the demo by writing a different
        # manifest under the same course_id.
        from axiom.extensions.builtins.classroom.operational_store import _reg

        old = load_course_data(DEMO_COURSE_ID)
        mucked = dict(old)
        mucked["manifest"] = {"title": "User-mucked"}
        _reg().register(kind="course", name=DEMO_COURSE_ID, data=mucked)
        assert load_course_data(DEMO_COURSE_ID)["manifest"]["title"] == "User-mucked"

        reset_demo()
        assert "Classical Mechanics" in load_course_data(DEMO_COURSE_ID)["title"]

    def test_reset_on_empty_is_safe(self):
        # Calling reset when no demo artifacts exist should just seed.
        reset_demo()
        assert load_course_data(DEMO_COURSE_ID) is not None
        assert load_classroom_data(DEMO_CLASSROOM_ID) is not None


class TestFromDemo:
    def test_clones_course_manifest_to_new_id(self):
        from axiom.extensions.builtins.classroom.demo import clone_demo_course

        seed_demo()
        new_id = clone_demo_course(new_course_id="my-course", instructor_id="@ben:ut")
        cloned = load_course_data(new_id)
        assert cloned is not None
        assert cloned["id"] == "my-course"
        assert cloned["instructor_id"] == "@ben:ut"
        # Title mirrors demo but without "(Demo)" marker — it's now THEIR course
        assert "(Demo)" not in cloned.get("title", "")

    def test_clone_preserves_corpus_prompt_assessments_rails(self):
        from axiom.extensions.builtins.classroom.demo import clone_demo_course

        seed_demo()
        demo = load_course_data(DEMO_COURSE_ID)
        new_id = clone_demo_course(new_course_id="my-course", instructor_id="@ben:ut")
        cloned = load_course_data(new_id)

        # Manifest, prompt, assessments, rails are copied
        assert cloned["system_prompt"] == demo["system_prompt"]
        assert cloned["assessments"] == demo["assessments"]
        assert cloned["rails"] == demo["rails"]
        assert cloned["corpus_doc_count"] == demo["corpus_doc_count"]

    def test_clone_does_not_modify_demo(self):
        from axiom.extensions.builtins.classroom.demo import clone_demo_course

        seed_demo()
        before = load_course_data(DEMO_COURSE_ID)
        clone_demo_course(new_course_id="my-course", instructor_id="@ben:ut")
        after = load_course_data(DEMO_COURSE_ID)
        assert before["manifest"] == after["manifest"]
        assert before["title"] == after["title"]

    def test_cloned_course_is_editable(self):
        """Cloned course is independent: saving a modified version to the
        new id must not affect the demo course."""
        from axiom.extensions.builtins.classroom.demo import clone_demo_course
        from axiom.extensions.builtins.classroom.operational_store import _reg

        seed_demo()
        new_id = clone_demo_course(new_course_id="my-course", instructor_id="@ben:ut")

        cloned = load_course_data(new_id)
        cloned["manifest"] = dict(cloned["manifest"])
        cloned["manifest"]["title"] = "My Custom Title"
        _reg().register(kind="course", name=new_id, data=cloned)

        assert load_course_data(new_id)["manifest"]["title"] == "My Custom Title"
        assert "Classical Mechanics" in load_course_data(DEMO_COURSE_ID)["title"]

    def test_clone_rejects_colliding_course_id(self):
        from axiom.extensions.builtins.classroom.demo import clone_demo_course

        seed_demo()
        clone_demo_course(new_course_id="my-course", instructor_id="@ben:ut")

        with pytest.raises(ValueError, match="already exists"):
            clone_demo_course(new_course_id="my-course", instructor_id="@ben:ut")

    def test_clone_rejects_demo_course_id(self):
        from axiom.extensions.builtins.classroom.demo import clone_demo_course

        with pytest.raises(ValueError, match="demo"):
            clone_demo_course(
                new_course_id=DEMO_COURSE_ID, instructor_id="@ben:ut"
            )


class TestCloneDemo:
    """``clone_demo`` — the CLI-facing clone that produces both course
    and classroom bound to each other (G1 fix)."""

    def test_produces_both_artifacts(self):
        from axiom.extensions.builtins.classroom.demo import clone_demo

        seed_demo()
        result = clone_demo(new_course_id="my-course", instructor_id="@ben:ut")
        assert result["course_id"] == "my-course"
        assert result["classroom_id"] == "my-course"  # default: match course id
        assert load_course_data("my-course") is not None
        assert load_classroom_data("my-course") is not None

    def test_explicit_classroom_id(self):
        from axiom.extensions.builtins.classroom.demo import clone_demo

        seed_demo()
        result = clone_demo(
            new_course_id="my-course",
            instructor_id="@ben:ut",
            new_classroom_id="my-course-spring-2026",
        )
        assert result["classroom_id"] == "my-course-spring-2026"
        assert load_classroom_data("my-course-spring-2026") is not None

    def test_classroom_bound_to_cloned_course(self):
        """The new classroom's course_id points at the new course, NOT
        the demo course. This is the G1 regression."""
        from axiom.extensions.builtins.classroom.demo import clone_demo

        seed_demo()
        clone_demo(new_course_id="my-course", instructor_id="@ben:ut")
        classroom = load_classroom_data("my-course")
        assert classroom["course_id"] == "my-course"
        assert classroom["course_id"] != DEMO_COURSE_ID

    def test_cloned_classroom_starts_unpublished(self):
        """Regardless of whether the demo classroom was ever published,
        the cloned classroom starts in UNPUBLISHED state so the instructor
        explicitly publishes when ready."""
        from axiom.extensions.builtins.classroom.demo import clone_demo

        seed_demo()
        # Put the demo classroom into PUBLISHED first to prove reset
        from axiom.extensions.builtins.classroom.publish import publish_classroom

        publish_classroom(classroom_id=DEMO_CLASSROOM_ID, approver="@ben:ut")

        clone_demo(new_course_id="my-course", instructor_id="@ben:ut")
        cloned_classroom = load_classroom_data("my-course")
        assert cloned_classroom.get("state", "unpublished") == "unpublished"
        assert cloned_classroom.get("published_at") is None

    def test_rejects_colliding_classroom_id(self):
        from axiom.extensions.builtins.classroom.demo import clone_demo

        seed_demo()
        clone_demo(new_course_id="my-course", instructor_id="@ben:ut")

        with pytest.raises(ValueError, match="already exists"):
            clone_demo(
                new_course_id="other-course",
                instructor_id="@ben:ut",
                new_classroom_id="my-course",  # collides with first clone
            )


class TestDemoContent:
    def test_corpus_is_classical_mechanics(self):
        """Domain-agnostic subject — classroom core ships without
        mentioning any specific consumer domain."""
        from axiom.extensions.builtins.classroom.demo import DEMO_CORPUS

        text = " ".join(d.get("text", "") for d in DEMO_CORPUS).lower()
        # Physics vocabulary expected
        physics_terms = ("force", "mass", "acceleration", "energy", "momentum")
        found = sum(1 for term in physics_terms if term in text)
        assert found >= 3, f"expected at least 3 physics terms, found {found}"

        # Explicitly avoid domain-consumer terms
        forbidden = ("nuclear", "reactor", "neutron", "fission")
        for term in forbidden:
            assert term not in text, f"demo corpus must not name domain consumer: {term}"

    def test_roster_is_five_students(self):
        from axiom.extensions.builtins.classroom.demo import DEMO_ROSTER

        assert len(DEMO_ROSTER) == 5

    def test_assessments_have_model_answers(self):
        from axiom.extensions.builtins.classroom.demo import DEMO_ASSESSMENTS

        for a in DEMO_ASSESSMENTS:
            questions = a.get("questions", [])
            assert questions, f"assessment {a.get('id')} has no questions"
            for q in questions:
                assert q.get("model_answer"), f"question {q.get('id')} lacks model_answer"


class TestDemoCoordinatorMaterials:
    """`seed_demo` must populate the coordinator-side materials store
    so a real student joining the demo cohort downloads + indexes
    materials on join. Without this, `axi classroom ask` after a
    student joins the demo classroom returns "no matching passages"
    and the skeptic-evaluation collapses."""

    def test_materials_seeded_in_coordinator_dir(self, tmp_path, monkeypatch):
        from axiom.extensions.builtins.classroom.classroom_materials import (
            ClassroomMaterialsStore,
        )
        from axiom.extensions.builtins.classroom.demo import (
            DEMO_CLASSROOM_ID,
            DEMO_CORPUS,
        )

        monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
        seed_demo()

        coord_dir = tmp_path / ".axi" / "coordinator" / "classrooms" / DEMO_CLASSROOM_ID
        assert coord_dir.is_dir()

        store = ClassroomMaterialsStore(coord_dir)
        entries = store.list_entries()
        # One entry per demo doc — re-seeding is idempotent on (filename,
        # content) so repeated runs don't grow the store.
        titles = {e.title for e in entries}
        for doc in DEMO_CORPUS:
            assert doc["title"] in titles

    def test_seed_demo_is_idempotent_for_materials(self, tmp_path, monkeypatch):
        from axiom.extensions.builtins.classroom.classroom_materials import (
            ClassroomMaterialsStore,
        )
        from axiom.extensions.builtins.classroom.demo import (
            DEMO_CLASSROOM_ID,
            DEMO_CORPUS,
        )

        monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
        seed_demo()
        seed_demo()  # second call should not duplicate entries

        coord_dir = tmp_path / ".axi" / "coordinator" / "classrooms" / DEMO_CLASSROOM_ID
        store = ClassroomMaterialsStore(coord_dir)
        entries = store.list_entries()
        # Idempotent on (filename, content) — count stays at len(DEMO_CORPUS).
        assert len(entries) == len(DEMO_CORPUS)
