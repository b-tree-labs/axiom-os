# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tests for onboarding rail customization — FW-1 P3b.

Rails are named, ordered sequences of onboarding questions that
auto-apply to new students. P3b lets instructors:

- Discover which question banks are installed (core + extensions)
- Add a new rail seeded from a bank (or from custom questions)
- Preview the rail as a stub student (``@alice:demo``)

Question banks are discovered via a simple registry: modules
registered under ``axiom.extensions.builtins.classroom.question_banks``
contribute banks. For P3b we ship one core bank (``axiom-core-starter``)
with a handful of generic onboarding questions. Domain extensions
will register their own banks in later phases.
"""

from __future__ import annotations

import pytest

from axiom.extensions.builtins.classroom.question_banks import (
    CORE_STARTER_BANK,
    QuestionBank,
    add_rail_from_bank,
    list_banks,
    preview_rail,
    register_bank,
    unregister_bank,
)

# ---------------------------------------------------------------------------
# Core bank shape
# ---------------------------------------------------------------------------


class TestCoreStarterBank:
    def test_ships_with_id(self):
        assert CORE_STARTER_BANK.id == "axiom-core-starter"

    def test_has_questions(self):
        assert len(CORE_STARTER_BANK.questions) >= 3

    def test_questions_have_required_fields(self):
        for q in CORE_STARTER_BANK.questions:
            assert q.get("id"), f"question missing id: {q}"
            assert q.get("prompt")
            assert q.get("response_type") in (
                "yes_no",
                "likert",
                "free_text",
                "multiple_choice",
            )

    def test_bank_is_domain_agnostic(self):
        text = " ".join(q.get("prompt", "") for q in CORE_STARTER_BANK.questions).lower()
        forbidden = ("nuclear", "reactor", "neutron", "fission")
        for term in forbidden:
            assert term not in text, (
                f"core bank must not name a domain consumer: {term}"
            )


# ---------------------------------------------------------------------------
# Bank registry
# ---------------------------------------------------------------------------


class TestBankRegistry:
    def test_list_banks_includes_core_starter(self):
        banks = list_banks()
        ids = {b.id for b in banks}
        assert "axiom-core-starter" in ids

    def test_register_and_unregister_custom_bank(self):
        custom = QuestionBank(
            id="test-custom",
            description="Test bank",
            questions=[
                {"id": "tc1", "prompt": "Test?", "response_type": "yes_no"},
            ],
        )
        register_bank(custom)
        try:
            assert any(b.id == "test-custom" for b in list_banks())
        finally:
            unregister_bank("test-custom")
        assert not any(b.id == "test-custom" for b in list_banks())

    def test_register_rejects_duplicate_id(self):
        custom = QuestionBank(id="dup", description="x", questions=[
            {"id": "q", "prompt": "Q?", "response_type": "yes_no"},
        ])
        register_bank(custom)
        try:
            with pytest.raises(ValueError, match="already registered"):
                register_bank(custom)
        finally:
            unregister_bank("dup")


# ---------------------------------------------------------------------------
# add_rail_from_bank
# ---------------------------------------------------------------------------


class TestAddRailFromBank:
    def test_adds_rail_seeded_from_bank(self):
        manifest: dict = {"id": "c1", "title": "T", "version": "1", "rails": []}
        rail = add_rail_from_bank(
            manifest,
            rail_id="intake",
            bank_id="axiom-core-starter",
        )
        assert rail["id"] == "intake"
        assert rail["source"] == "axiom-core-starter"
        assert len(rail["questions"]) == len(CORE_STARTER_BANK.questions)
        assert manifest["rails"][0] is rail

    def test_auto_apply_defaults_to_all_new_students(self):
        manifest: dict = {"id": "c1", "title": "T", "version": "1", "rails": []}
        rail = add_rail_from_bank(
            manifest, rail_id="intake", bank_id="axiom-core-starter",
        )
        assert rail["auto_apply_to"] == "all_new_students"

    def test_subset_via_question_ids(self):
        manifest: dict = {"id": "c1", "title": "T", "version": "1", "rails": []}
        sample_qid = CORE_STARTER_BANK.questions[0]["id"]
        rail = add_rail_from_bank(
            manifest,
            rail_id="intake-min",
            bank_id="axiom-core-starter",
            question_ids=[sample_qid],
        )
        assert len(rail["questions"]) == 1
        assert rail["questions"][0]["id"] == sample_qid

    def test_unknown_bank_raises(self):
        manifest: dict = {"id": "c1", "title": "T", "version": "1"}
        with pytest.raises(ValueError, match="bank"):
            add_rail_from_bank(
                manifest, rail_id="x", bank_id="nonexistent-bank",
            )

    def test_unknown_question_id_raises(self):
        manifest: dict = {"id": "c1", "title": "T", "version": "1"}
        with pytest.raises(ValueError, match="question"):
            add_rail_from_bank(
                manifest,
                rail_id="x",
                bank_id="axiom-core-starter",
                question_ids=["not-a-real-qid"],
            )

    def test_duplicate_rail_id_replaces_in_place(self):
        """Add with an existing rail_id replaces — natural update UX."""
        manifest: dict = {"id": "c1", "title": "T", "version": "1", "rails": []}
        add_rail_from_bank(
            manifest, rail_id="intake", bank_id="axiom-core-starter",
        )
        assert len(manifest["rails"]) == 1
        sample_qid = CORE_STARTER_BANK.questions[0]["id"]
        second = add_rail_from_bank(
            manifest,
            rail_id="intake",
            bank_id="axiom-core-starter",
            question_ids=[sample_qid],
        )
        assert len(manifest["rails"]) == 1
        assert len(second["questions"]) == 1


# ---------------------------------------------------------------------------
# preview_rail — stub-student preview
# ---------------------------------------------------------------------------


class TestPreviewRail:
    def test_returns_stub_session(self):
        manifest: dict = {"id": "c1", "title": "T", "version": "1", "rails": []}
        add_rail_from_bank(
            manifest, rail_id="intake", bank_id="axiom-core-starter",
        )
        session = preview_rail(manifest, rail_id="intake")
        assert session["rail_id"] == "intake"
        assert session["student_persona"] == "@alice:demo"
        assert len(session["turns"]) == len(CORE_STARTER_BANK.questions)

    def test_turns_include_prompt_and_sample_response(self):
        manifest: dict = {"id": "c1", "title": "T", "version": "1", "rails": []}
        add_rail_from_bank(
            manifest, rail_id="intake", bank_id="axiom-core-starter",
        )
        session = preview_rail(manifest, rail_id="intake")
        for turn in session["turns"]:
            assert turn.get("question_id")
            assert turn.get("prompt")
            assert turn.get("sample_response")

    def test_unknown_rail_raises(self):
        manifest: dict = {"id": "c1", "title": "T", "version": "1", "rails": []}
        with pytest.raises(ValueError, match="rail"):
            preview_rail(manifest, rail_id="nope")


# ---------------------------------------------------------------------------
# CLI — axi classroom prep rails {list-banks|add|preview}
# ---------------------------------------------------------------------------


class TestRailsCLI:
    @pytest.fixture(autouse=True)
    def _isolated_runtime(self, tmp_path, monkeypatch):
        monkeypatch.setenv("AXIOM_RUNTIME_ROOT", str(tmp_path))
        import axiom.extensions.builtins.classroom.operational_store as store

        store._registry = None
        yield
        store._registry = None

    @pytest.fixture
    def seeded_course(self):
        from axiom.extensions.builtins.classroom.demo import (
            clone_demo_course,
            seed_demo,
        )

        seed_demo()
        clone_demo_course(new_course_id="my-course", instructor_id="@ben:ut")
        return "my-course"

    def test_list_banks_json(self, capsys):
        import json

        from axiom.extensions.builtins.classroom.cli import main

        rc = main(["prep", "rails", "list-banks", "--json"])
        assert rc == 0
        data = json.loads(capsys.readouterr().out)
        assert any(b["id"] == "axiom-core-starter" for b in data["banks"])

    def test_add_bank_rail(self, seeded_course, capsys):
        import json

        from axiom.extensions.builtins.classroom.cli import main
        from axiom.extensions.builtins.classroom.operational_store import (
            load_course_data,
        )

        rc = main(
            [
                "prep", "rails", "add", seeded_course,
                "--rail-id", "intake",
                "--bank", "axiom-core-starter",
                "--json",
            ]
        )
        assert rc == 0
        data = json.loads(capsys.readouterr().out)
        assert data["added"]["id"] == "intake"
        # Persisted
        course_data = load_course_data(seeded_course)
        rail_ids = [r["id"] for r in course_data["manifest"].get("rails", [])]
        assert "intake" in rail_ids

    def test_add_rail_with_question_subset(self, seeded_course, capsys):
        import json

        from axiom.extensions.builtins.classroom.cli import main

        qid = CORE_STARTER_BANK.questions[0]["id"]
        rc = main(
            [
                "prep", "rails", "add", seeded_course,
                "--rail-id", "intake-min",
                "--bank", "axiom-core-starter",
                "--ids", qid,
                "--json",
            ]
        )
        assert rc == 0
        data = json.loads(capsys.readouterr().out)
        assert len(data["added"]["questions"]) == 1

    def test_add_rejects_unknown_bank(self, seeded_course, capsys):
        import json

        from axiom.extensions.builtins.classroom.cli import main

        rc = main(
            [
                "prep", "rails", "add", seeded_course,
                "--rail-id", "x",
                "--bank", "nope",
                "--json",
            ]
        )
        assert rc == 1
        data = json.loads(capsys.readouterr().out)
        assert "error" in data

    def test_preview_rail(self, seeded_course, capsys):
        import json

        from axiom.extensions.builtins.classroom.cli import main

        main(
            [
                "prep", "rails", "add", seeded_course,
                "--rail-id", "intake",
                "--bank", "axiom-core-starter",
                "--json",
            ]
        )
        capsys.readouterr()
        rc = main(
            [
                "prep", "rails", "preview", seeded_course,
                "--rail-id", "intake",
                "--json",
            ]
        )
        assert rc == 0
        data = json.loads(capsys.readouterr().out)
        assert data["student_persona"] == "@alice:demo"
        assert data["turns"]


# ---------------------------------------------------------------------------
# Chat tool — classroom_prep_configure_rails
# ---------------------------------------------------------------------------


class TestRailsChatTool:
    @pytest.fixture(autouse=True)
    def _isolated_runtime(self, tmp_path, monkeypatch):
        monkeypatch.setenv("AXIOM_RUNTIME_ROOT", str(tmp_path))
        import axiom.extensions.builtins.classroom.operational_store as store

        store._registry = None
        yield
        store._registry = None

    @pytest.fixture
    def seeded_course(self):
        from axiom.extensions.builtins.classroom.demo import (
            clone_demo_course,
            seed_demo,
        )

        seed_demo()
        clone_demo_course(new_course_id="my-course", instructor_id="@ben:ut")
        return "my-course"

    def test_tool_registered(self):
        from axiom.extensions.builtins.classroom.chat_tools import prep_tools

        names = {t.name for t in prep_tools.TOOLS}
        assert "classroom_prep_configure_rails" in names

    def test_list_banks_action(self, seeded_course):
        from axiom.extensions.builtins.classroom.chat_tools import prep_tools

        result = prep_tools.execute(
            "classroom_prep_configure_rails",
            {"action": "list-banks"},
        )
        assert any(b["id"] == "axiom-core-starter" for b in result["banks"])

    def test_add_action(self, seeded_course):
        from axiom.extensions.builtins.classroom.chat_tools import prep_tools

        result = prep_tools.execute(
            "classroom_prep_configure_rails",
            {
                "action": "add",
                "course_id": seeded_course,
                "rail_id": "intake",
                "bank_id": "axiom-core-starter",
            },
        )
        assert result["added"]["id"] == "intake"

    def test_preview_action(self, seeded_course):
        from axiom.extensions.builtins.classroom.chat_tools import prep_tools

        prep_tools.execute(
            "classroom_prep_configure_rails",
            {
                "action": "add",
                "course_id": seeded_course,
                "rail_id": "intake",
                "bank_id": "axiom-core-starter",
            },
        )
        result = prep_tools.execute(
            "classroom_prep_configure_rails",
            {
                "action": "preview",
                "course_id": seeded_course,
                "rail_id": "intake",
            },
        )
        assert result["student_persona"] == "@alice:demo"
        assert result["turns"]

    def test_bad_action(self, seeded_course):
        from axiom.extensions.builtins.classroom.chat_tools import prep_tools

        result = prep_tools.execute(
            "classroom_prep_configure_rails",
            {"action": "nuke", "course_id": seeded_course},
        )
        assert "error" in result
