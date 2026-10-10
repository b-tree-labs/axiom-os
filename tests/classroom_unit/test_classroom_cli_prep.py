# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""TDD tests for `axi classroom prep` CLI — unified flow.

Architecture:
- Standalone flow (default): instructor walks all steps manually.
- LMS-assisted (optional): --lms pre-populates corpus/assessments/roster
  from the connected LMS; instructor still authors prompt + RAG policy.
- Course reuse (optional): --from <prior-classroom-or-course> skips
  course prep entirely, starts classroom prep at RAG policy.

IDs are auto-generated (uuid + slug); instructor never types an ID.
Commands receive the classroom_id returned from `init`.

Spec: spec-classroom.md §2.6. Feedback:
feedback_auto_generated_ids.md, feedback_standalone_first_lms_optional.md.
"""

from __future__ import annotations

import re

import pytest


@pytest.fixture
def runtime_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("AXIOM_RUNTIME_ROOT", str(tmp_path / "runtime"))
    return tmp_path / "runtime"


def _init_and_get_classroom_id(capsys, runtime_dir, **kwargs) -> str:
    """Run `prep init` and extract the generated classroom id."""
    from axiom.extensions.builtins.classroom.cli import main

    argv = ["prep", "init", "--instructor", kwargs.pop("instructor", "ben@ut.edu")]
    for k, v in kwargs.items():
        argv.append(f"--{k.replace('_', '-')}")
        if v is not True:
            argv.append(str(v))

    rc = main(argv)
    assert rc == 0
    out = capsys.readouterr().out

    # Look for "Classroom: <slug>  (id: <uuid>)"
    m = re.search(r"Classroom:\s+(\S+)\s+\(id: ([0-9a-f-]{36})\)", out)
    assert m is not None, f"init output did not include classroom id:\n{out}"
    return m.group(2)


# ---------------------------------------------------------------------------


class TestStatusOnMissing:
    def test_status_before_init_is_nonzero(self, runtime_dir, capsys):
        from axiom.extensions.builtins.classroom.cli import main

        rc = main(["prep", "status", "nonexistent-classroom-id"])
        assert rc != 0
        out = capsys.readouterr().out
        assert "no classroom prep session" in out.lower()


class TestInitStandalone:
    def test_init_generates_ids_and_files(self, runtime_dir, capsys):
        from axiom.extensions.builtins.classroom.operational_store import (
            load_classroom,
            load_course,
        )

        classroom_id = _init_and_get_classroom_id(
            capsys, runtime_dir, title="NE Prague 2026"
        )

        # State persisted via ArtifactRegistry (post-#74)
        loaded = load_classroom(classroom_id)
        assert loaded is not None
        _, classroom_data = loaded
        assert classroom_data["instructor_id"] == "ben@ut.edu"
        assert classroom_data["id"] == classroom_id

        course_id = classroom_data["course_id"]
        course_loaded = load_course(course_id)
        assert course_loaded is not None

    def test_init_slug_derived_from_title(self, runtime_dir, capsys):
        from axiom.extensions.builtins.classroom.cli import main

        rc = main(["prep", "init", "--instructor", "i", "--title", "Hello World"])
        assert rc == 0
        out = capsys.readouterr().out
        # Both slugs should start with "hello-world-"
        assert "hello-world-" in out

    def test_init_without_title_uses_untitled(self, runtime_dir, capsys):
        from axiom.extensions.builtins.classroom.cli import main

        rc = main(["prep", "init", "--instructor", "i"])
        assert rc == 0
        out = capsys.readouterr().out
        assert "untitled-" in out


class TestStatusAfterInit:
    def test_status_shows_course_and_classroom(self, runtime_dir, capsys):
        classroom_id = _init_and_get_classroom_id(capsys, runtime_dir, title="x")
        capsys.readouterr()  # drain

        rc = main_status(classroom_id)
        out = capsys.readouterr().out
        # Not ready — classroom needs RAG + LMS, course needs corpus + prompt
        assert rc == 2
        assert "Course " in out or "course" in out.lower()
        assert "Classroom " in out or "classroom" in out.lower()


def main_status(classroom_id):
    from axiom.extensions.builtins.classroom.cli import main

    return main(["prep", "status", classroom_id])


class TestCorpusStep:
    def test_upload_and_preview(self, runtime_dir, tmp_path, capsys):
        from axiom.extensions.builtins.classroom.cli import main
        from axiom.extensions.builtins.classroom.operational_store import (
            load_classroom,
            load_course,
        )

        classroom_id = _init_and_get_classroom_id(capsys, runtime_dir, title="c")
        capsys.readouterr()

        doc = tmp_path / "ch1.txt"
        doc.write_text("fission splits heavy nuclei")

        rc = main([
            "prep", "corpus", classroom_id,
            "--upload", str(doc),
            "--preview", "fission",
        ])
        assert rc == 0

        _, classroom_data = load_classroom(classroom_id)
        _, course_data = load_course(classroom_data["course_id"])
        assert course_data["steps"][1]["status"] in ("completed", "warning")

    def test_upload_persists_to_coordinator_materials_dir(
        self, runtime_dir, tmp_path, capsys, monkeypatch,
    ):
        """Phase 1: every prep-corpus upload lands on disk so Phase 2 can
        serve it to joining students. Before this PR the docs lived only
        in memory and disappeared at CLI exit."""
        from axiom.extensions.builtins.classroom.classroom_materials import (
            ClassroomMaterialsStore,
        )
        from axiom.extensions.builtins.classroom.cli import main

        # Redirect $HOME so the coordinator state dir lands under tmp.
        monkeypatch.setenv("HOME", str(tmp_path / "home"))

        classroom_id = _init_and_get_classroom_id(capsys, runtime_dir, title="c")
        capsys.readouterr()

        doc = tmp_path / "syllabus.md"
        doc.write_text("# Syllabus\nFission splits heavy nuclei.\n")

        rc = main([
            "prep", "corpus", classroom_id,
            "--upload", str(doc),
        ])
        assert rc == 0

        # Materials store is readable from a fresh instance — proof of disk.
        store = ClassroomMaterialsStore(
            tmp_path / "home" / ".axi" / "coordinator"
            / "classrooms" / classroom_id
        )
        entries = store.list_entries()
        assert len(entries) == 1
        assert entries[0].filename == "syllabus.md"
        assert store.get_path(entries[0].file_id).read_text().startswith("# Syllabus")


class TestPromptStep:
    def test_set_test_approve_and_autoselect_course(self, runtime_dir, capsys):
        from axiom.extensions.builtins.classroom.cli import main
        from axiom.extensions.builtins.classroom.operational_store import (
            load_classroom,
            load_course,
        )

        classroom_id = _init_and_get_classroom_id(capsys, runtime_dir, title="c")
        capsys.readouterr()

        rc = main([
            "prep", "prompt", classroom_id,
            "--set", "You are a tutor.",
            "--test", "What is fission?",
        ])
        assert rc == 0

        rc = main(["prep", "prompt", classroom_id, "--approve"])
        assert rc == 0

        _, classroom_data = load_classroom(classroom_id)
        _, course_data = load_course(classroom_data["course_id"])
        assert course_data["steps"][2]["status"] == "completed"


class TestRAGPolicy:
    def test_select_mode(self, runtime_dir, capsys):
        from axiom.extensions.builtins.classroom.cli import main
        from axiom.extensions.builtins.classroom.operational_store import load_classroom

        classroom_id = _init_and_get_classroom_id(capsys, runtime_dir, title="c")
        capsys.readouterr()

        rc = main(["prep", "rag", classroom_id, "--mode", "course_only"])
        assert rc == 0
        _, classroom_data = load_classroom(classroom_id)
        assert classroom_data["steps"][1]["status"] == "completed"

    def test_invalid_mode_fails(self, runtime_dir, capsys):
        from axiom.extensions.builtins.classroom.cli import main

        classroom_id = _init_and_get_classroom_id(capsys, runtime_dir, title="c")
        capsys.readouterr()

        rc = main(["prep", "rag", classroom_id, "--mode", "nonsense"])
        assert rc != 0


class TestLMSStep:
    def test_fake_lms_connect(self, runtime_dir, capsys):
        from axiom.extensions.builtins.classroom.cli import main
        from axiom.extensions.builtins.classroom.operational_store import load_classroom

        classroom_id = _init_and_get_classroom_id(capsys, runtime_dir, title="c")
        capsys.readouterr()

        rc = main([
            "prep", "lms", classroom_id,
            "--canvas-course", "101",
            "--fake", "--fake-roster", "3",
        ])
        assert rc == 0

        _, classroom_data = load_classroom(classroom_id)
        assert classroom_data["steps"][2]["status"] == "completed"


class TestEndToEndReadiness:
    def test_full_flow_marks_ready(self, runtime_dir, tmp_path, capsys):
        from axiom.extensions.builtins.classroom.cli import main

        classroom_id = _init_and_get_classroom_id(capsys, runtime_dir, title="c")
        doc = tmp_path / "d.txt"
        doc.write_text("nuclear fundamentals")

        main(["prep", "corpus", classroom_id, "--upload", str(doc), "--preview", "nuclear"])
        main(["prep", "prompt", classroom_id, "--set", "Tutor.", "--test", "hi"])
        main(["prep", "prompt", classroom_id, "--approve"])
        main(["prep", "rag", classroom_id, "--mode", "course_only"])
        main([
            "prep", "lms", classroom_id,
            "--canvas-course", "101",
            "--fake", "--fake-roster", "2",
        ])

        capsys.readouterr()
        rc = main(["prep", "status", classroom_id])
        out = capsys.readouterr().out
        assert rc == 0
        assert "ready" in out.lower()


class TestLMSFirstInit:
    """Optional: --lms-assist pre-populates corpus/assessments/roster.

    Standalone still works; LMS is a shortcut, not a requirement.
    """

    def test_init_with_fake_lms_assist_prefills_steps(self, runtime_dir, capsys):
        from axiom.extensions.builtins.classroom.cli import main

        rc = main([
            "prep", "init",
            "--instructor", "i",
            "--title", "Auto Course",
            "--lms-assist-fake",  # test-only flag simulating Canvas import
        ])
        assert rc == 0
        out = capsys.readouterr().out
        # Output should mention the LMS import happening
        assert "canvas" in out.lower() or "lms" in out.lower() or "imported" in out.lower()

        # Corpus + assessments + roster should be pre-populated
        m = re.search(r"Classroom:\s+\S+\s+\(id: ([0-9a-f-]{36})\)", out)
        assert m is not None
        classroom_id = m.group(1)

        from axiom.extensions.builtins.classroom.operational_store import (
            load_classroom,
            load_course,
        )

        _, classroom_data = load_classroom(classroom_id)
        _, course_data = load_course(classroom_data["course_id"])
        # Corpus step advanced
        assert course_data["steps"][1]["status"] in ("completed", "warning")
        # Assessment step advanced
        assert course_data["steps"][3]["status"] == "completed"
        # Classroom LMS step advanced
        assert classroom_data["steps"][2]["status"] == "completed"


class TestReuseFlow:
    def test_init_from_prior_classroom_skips_course_prep(self, runtime_dir, tmp_path, capsys):
        from axiom.extensions.builtins.classroom.cli import main

        # Build a first classroom with a completed course
        first_id = _init_and_get_classroom_id(capsys, runtime_dir, title="prior")
        doc = tmp_path / "d.txt"
        doc.write_text("stuff")
        main(["prep", "corpus", first_id, "--upload", str(doc), "--preview", "stuff"])
        main(["prep", "prompt", first_id, "--set", "tutor", "--test", "hi"])
        main(["prep", "prompt", first_id, "--approve"])
        capsys.readouterr()

        # Now init a new classroom reusing the prior course
        rc = main([
            "prep", "init",
            "--instructor", "i",
            "--title", "second semester",
            "--from", first_id,
        ])
        assert rc == 0
        out = capsys.readouterr().out
        assert "reusing" in out.lower()

        # Extract new classroom id
        m = re.search(r"Classroom:\s+\S+\s+\(id: ([0-9a-f-]{36})\)", out)
        assert m is not None
        new_classroom_id = m.group(1)

        # New classroom's course-selected step should be already complete
        from axiom.extensions.builtins.classroom.operational_store import load_classroom

        _, classroom_data = load_classroom(new_classroom_id)
        assert classroom_data["steps"][0]["status"] == "completed"
