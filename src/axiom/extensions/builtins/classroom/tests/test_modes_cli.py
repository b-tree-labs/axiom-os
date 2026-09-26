# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tests for the learning-mode plumbing through the CLI.

Two halves: the instructor-side `axi classroom modes` command (show
+ mutate the classroom policy) and the student-side `axi classroom
ask --mode` flag that respects the policy.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from axiom.extensions.builtins.classroom.classroom_federation import (
    create_cohort,
)
from axiom.extensions.builtins.classroom.classroom_local_index import (
    ClassroomLocalIndex,
)
from axiom.extensions.builtins.classroom.cli import main
from axiom.extensions.builtins.classroom.coordinator_cohort_store import (
    FileCohortStore,
)
from axiom.extensions.builtins.classroom.learning_modes import (
    ClassroomModePolicy,
)
from axiom.vega.federation.identity import generate_identity

# ---------------------------------------------------------------------------
# Instructor side: axi classroom modes
# ---------------------------------------------------------------------------


@pytest.fixture
def instructor_home(tmp_path, monkeypatch):
    home = tmp_path / "instructor"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(
        "axiom.vega.federation.identity._DEFAULT_KEYS_DIR",
        home / "identity",
    )
    # Bootstrap a class.
    identity = generate_identity(
        owner="prof@ut.edu", keys_dir=home / "identity",
    )
    coord_dir = home / ".axi" / "coordinator"
    cohort_store = FileCohortStore(coord_dir)
    cohort_store.save(
        create_cohort("NE101", identity.node_id),
        coordinator_url="http://placeholder/classroom/join",
    )
    return home


class TestInstructorModesRead:
    def test_modes_show_lists_all_modes_by_default(
        self, instructor_home, capsys,
    ):
        rc = main(["modes", "NE101"])
        assert rc == 0
        out = capsys.readouterr().out
        # All shipped modes mentioned.
        for name in ("ask", "tutor", "quiz", "reflect", "review"):
            assert name in out
        # Default policy → nothing forced.
        assert "Forced" not in out

    def test_modes_json_reflects_current_policy(
        self, instructor_home, capsys,
    ):
        rc = main(["modes", "NE101", "--json"])
        assert rc == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["classroom_id"] == "NE101"
        assert "ask" in payload["policy"]["allowed_modes"]
        assert payload["policy"]["forced_mode"] is None


class TestInstructorModesWrite:
    def test_allow_narrows_the_set(
        self, instructor_home, capsys,
    ):
        rc = main(["modes", "NE101", "--allow", "ask,reflect", "--json"])
        assert rc == 0
        payload = json.loads(capsys.readouterr().out)
        assert set(payload["policy"]["allowed_modes"]) == {"ask", "reflect"}

    def test_allow_all_resets_to_everything(
        self, instructor_home, capsys,
    ):
        main(["modes", "NE101", "--allow", "ask"])  # narrow first
        capsys.readouterr()
        rc = main(["modes", "NE101", "--allow", "all", "--json"])
        assert rc == 0
        payload = json.loads(capsys.readouterr().out)
        assert {"ask", "tutor", "quiz", "reflect", "review"} <= set(
            payload["policy"]["allowed_modes"]
        )

    def test_force_mode_sets_override(
        self, instructor_home, capsys,
    ):
        rc = main(["modes", "NE101", "--force", "quiz", "--json"])
        assert rc == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["policy"]["forced_mode"] == "quiz"

    def test_force_none_clears_override(
        self, instructor_home, capsys,
    ):
        main(["modes", "NE101", "--force", "quiz"])
        capsys.readouterr()
        rc = main(["modes", "NE101", "--force", "none", "--json"])
        assert rc == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["policy"]["forced_mode"] is None

    def test_force_unknown_mode_errors(self, instructor_home, capsys):
        rc = main(["modes", "NE101", "--force", "xyzzy"])
        assert rc == 1
        err = capsys.readouterr().err
        assert "unknown mode" in err.lower()

    def test_force_must_be_in_allowed_set(self, instructor_home, capsys):
        main(["modes", "NE101", "--allow", "ask,reflect"])
        capsys.readouterr()
        rc = main(["modes", "NE101", "--force", "quiz"])
        assert rc == 1
        err = capsys.readouterr().err
        assert "allowed" in err.lower() or "add it first" in err.lower()

    def test_allow_unknown_mode_errors(self, instructor_home, capsys):
        rc = main(["modes", "NE101", "--allow", "ask,xyzzy"])
        assert rc == 1
        err = capsys.readouterr().err
        assert "unknown" in err.lower()


class TestModesErrors:
    def test_modes_on_unknown_class(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setenv("HOME", str(tmp_path / "empty"))
        rc = main(["modes", "NE999"])
        assert rc == 1
        err = capsys.readouterr().err
        assert "NE999" in err
        assert "invite" in err.lower()


# ---------------------------------------------------------------------------
# Student side: ask --mode
# ---------------------------------------------------------------------------


def _seed_student_class(home: Path, classroom_id: str = "NE101") -> Path:
    class_dir = home / ".axi" / "classrooms" / classroom_id
    class_dir.mkdir(parents=True, exist_ok=True)
    idx = ClassroomLocalIndex(base_dir=class_dir)
    idx.open()
    try:
        idx.ingest(
            file_id="f1",
            title="Chapter 1 — Control rods",
            content=(
                "Control rods absorb neutrons to slow fission reactions. "
                "They are made of boron or cadmium."
            ),
            embed=None,
        )
    finally:
        idx.close()
    return class_dir


class TestAskMode:
    def test_ask_default_mode_is_ask(self, tmp_path, monkeypatch, capsys):
        home = tmp_path / "student"
        monkeypatch.setenv("HOME", str(home))
        _seed_student_class(home)
        rc = main(["ask", "NE101", "control rod", "--json"])
        assert rc == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["mode"] == "ask"

    def test_student_can_pick_tutor_mode(
        self, tmp_path, monkeypatch, capsys,
    ):
        home = tmp_path / "student"
        monkeypatch.setenv("HOME", str(home))
        _seed_student_class(home)
        rc = main([
            "ask", "NE101", "control rod",
            "--mode", "tutor", "--json",
        ])
        assert rc == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["mode"] == "tutor"

    def test_student_mode_preference_persists(
        self, tmp_path, monkeypatch, capsys,
    ):
        home = tmp_path / "student"
        monkeypatch.setenv("HOME", str(home))
        class_dir = _seed_student_class(home)

        # First ask — pick tutor explicitly.
        main(["ask", "NE101", "x", "--mode", "tutor", "--json"])
        capsys.readouterr()
        # Preference file written.
        pref = (class_dir / "mode_preference.txt").read_text().strip()
        assert pref == "tutor"

        # Next ask with no flag — should still be tutor.
        main(["ask", "NE101", "y", "--json"])
        payload = json.loads(capsys.readouterr().out)
        assert payload["mode"] == "tutor"

    def test_instructor_forced_mode_overrides_student_choice(
        self, tmp_path, monkeypatch, capsys,
    ):
        """Policy cached to disk says forced=quiz. Student asks with
        --mode tutor; effective mode must become quiz."""
        home = tmp_path / "student"
        monkeypatch.setenv("HOME", str(home))
        class_dir = _seed_student_class(home)

        # Simulate a policy cache showing the instructor has forced
        # "quiz" (as if fetched from the coordinator before this call).
        policy = ClassroomModePolicy(
            allowed_modes=frozenset({"ask", "tutor", "quiz"}),
            forced_mode="quiz",
        )
        (class_dir / "policy.json").write_text(
            json.dumps(policy.to_dict())
        )
        # No coordinator_url sidecar → resolve_policy falls back to cache.

        rc = main([
            "ask", "NE101", "control rod",
            "--mode", "tutor", "--json",
        ])
        assert rc == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["mode"] == "quiz"

    def test_disallowed_mode_falls_back_to_default(
        self, tmp_path, monkeypatch, capsys,
    ):
        home = tmp_path / "student"
        monkeypatch.setenv("HOME", str(home))
        class_dir = _seed_student_class(home)

        policy = ClassroomModePolicy(
            allowed_modes=frozenset({"ask", "reflect"}),
            forced_mode=None,
        )
        (class_dir / "policy.json").write_text(
            json.dumps(policy.to_dict())
        )

        rc = main([
            "ask", "NE101", "control rod",
            "--mode", "quiz", "--json",  # not allowed
        ])
        assert rc == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["mode"] in {"ask", "reflect"}

    def test_quiz_mode_returns_no_citations_even_with_index(
        self, tmp_path, monkeypatch, capsys,
    ):
        """Quiz mode is closed-book — retrieval MUST be skipped even
        if the local index would happily match."""
        home = tmp_path / "student"
        monkeypatch.setenv("HOME", str(home))
        _seed_student_class(home)

        rc = main([
            "ask", "NE101", "control rod",
            "--mode", "quiz", "--json",
        ])
        assert rc == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["mode"] == "quiz"
        assert payload["citations"] == []
        assert payload["answer"] == ""
