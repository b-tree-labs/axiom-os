# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tests for classroom publish + enhanced dry-run — FW-1 P5.

Publish transitions a classroom from prep → active, gated on both
course-ready AND classroom-ready being true. Once active, the
classroom is considered bound to its course version and ready for
student enrollment.

Enhanced dry-run uses the course's actual corpus (not the stub
retriever) so the instructor sees a realistic student-turn before
publishing.
"""

from __future__ import annotations

import pytest

from axiom.extensions.builtins.classroom.publish import (
    PUBLISHED,
    UNPUBLISHED,
    enhanced_dry_run,
    get_classroom_state,
    publish_classroom,
    unpublish_classroom,
)


@pytest.fixture(autouse=True)
def _isolated_runtime(tmp_path, monkeypatch):
    monkeypatch.setenv("AXIOM_RUNTIME_ROOT", str(tmp_path))
    import axiom.extensions.builtins.classroom.operational_store as store

    store._registry = None
    yield
    store._registry = None


@pytest.fixture
def demo_classroom():
    from axiom.extensions.builtins.classroom.demo import (
        DEMO_CLASSROOM_ID,
        seed_demo,
    )

    seed_demo()
    return DEMO_CLASSROOM_ID


# ---------------------------------------------------------------------------
# State lifecycle
# ---------------------------------------------------------------------------


class TestClassroomState:
    def test_default_state_is_unpublished(self, demo_classroom):
        assert get_classroom_state(demo_classroom) == UNPUBLISHED

    def test_publish_transitions_to_published(self, demo_classroom):
        result = publish_classroom(
            classroom_id=demo_classroom, approver="@ben:ut",
        )
        assert result["published"] is True
        assert result["state"] == PUBLISHED
        assert get_classroom_state(demo_classroom) == PUBLISHED

    def test_publish_records_approver_and_timestamp(self, demo_classroom):
        result = publish_classroom(
            classroom_id=demo_classroom, approver="@ben:ut",
        )
        assert result["approver"] == "@ben:ut"
        assert result["published_at"]

    def test_unpublish_returns_to_unpublished(self, demo_classroom):
        publish_classroom(classroom_id=demo_classroom, approver="@ben:ut")
        unpublish_classroom(classroom_id=demo_classroom)
        assert get_classroom_state(demo_classroom) == UNPUBLISHED

    def test_unknown_classroom_fails(self):
        result = publish_classroom(classroom_id="nope", approver="x")
        assert result["published"] is False
        assert "error" in result


# ---------------------------------------------------------------------------
# Publish gating
# ---------------------------------------------------------------------------


class TestPublishGating:
    def test_demo_classroom_is_publishable(self, demo_classroom):
        """Demo ships with a fully-green checklist so publish succeeds."""
        result = publish_classroom(
            classroom_id=demo_classroom, approver="@ben:ut",
        )
        assert result["published"] is True

    def test_incomplete_classroom_is_rejected(self, tmp_path, monkeypatch):
        """Publish must refuse when course OR classroom isn't ready."""
        # Build a fresh classroom manually without running all prep steps.
        from axiom.extensions.builtins.classroom.classroom_prep_workflow import (
            ClassroomPrepWorkflow,
        )
        from axiom.extensions.builtins.classroom.cli import (
            _stub_llm,
            _StubLMSOffline,
            _StubRetriever,
        )
        from axiom.extensions.builtins.classroom.operational_store import (
            save_classroom,
        )

        wf = ClassroomPrepWorkflow(
            instructor_id="@x:demo",
            classroom_id="half-done",
            retriever=_StubRetriever([]),
            llm=_stub_llm,
            lms=_StubLMSOffline(roster_size=0),
        )
        save_classroom(
            wf, slug="half-done", title="Half Done",
            course_id="no-such-course", course_slug="x",
        )

        result = publish_classroom(
            classroom_id="half-done", approver="@ben:ut",
        )
        assert result["published"] is False
        assert "not ready" in result.get("error", "").lower()


# ---------------------------------------------------------------------------
# Enhanced dry-run
# ---------------------------------------------------------------------------


class TestEnhancedDryRun:
    def test_uses_course_corpus_for_retrieval(self, demo_classroom):
        """The demo course has a 10-doc classical-mechanics corpus;
        a query about Newton's second law should retrieve a hit."""
        result = enhanced_dry_run(
            classroom_id=demo_classroom,
            queries=["What is Newton's second law?"],
        )
        assert result["turns"] == 1
        transcript = result["transcript"]
        retrieved = transcript[0].get("retrieved", [])
        assert retrieved, "expected at least one retrieved doc for F=ma query"

    def test_default_queries_exercise_different_topics(self, demo_classroom):
        """With no queries supplied, the enhanced dry-run uses a small
        canned set so the instructor sees several turns at once."""
        result = enhanced_dry_run(classroom_id=demo_classroom)
        assert result["turns"] >= 2

    def test_transcript_entries_have_response(self, demo_classroom):
        result = enhanced_dry_run(classroom_id=demo_classroom)
        for turn in result["transcript"]:
            assert turn.get("query")
            assert turn.get("response")

    def test_unknown_classroom_returns_error(self):
        result = enhanced_dry_run(classroom_id="nope")
        assert result.get("error")


class TestEnhancedDryRunMaterialsCorpus:
    """The dry-run must retrieve from the instructor's actual uploaded
    materials, not the demo corpus. Earlier behavior pulled from the
    course manifest's ``corpus`` field, which the prep flow doesn't
    populate when the instructor uploads files via
    ``prep corpus --upload``. Without this, an instructor uploading
    reactor-physics docs got Newton's-laws answers in their dry-run."""

    def _make_real_classroom(self, tmp_path, monkeypatch):
        """Build a non-demo classroom with materials uploaded via the
        coordinator-side store (mirrors what `prep corpus --upload`
        does)."""
        from dataclasses import asdict

        from axiom.extensions.builtins.classroom.classroom_materials import (
            ClassroomMaterialsStore,
        )
        from axiom.extensions.builtins.classroom.classroom_prep import (
            create_classroom_prep_checklist,
            validate_course_selected_step,
        )
        from axiom.extensions.builtins.classroom.operational_store import _reg

        monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)

        course_id = "ne-101-test"
        classroom_id = "ne-101-spring"

        checklist = create_classroom_prep_checklist("@ben:ut", classroom_id)
        checklist = validate_course_selected_step(
            checklist,
            course_id=course_id,
            course_version="1.0.0",
            publishable=True,
        )

        reg = _reg()
        reg.register(kind="course", name=course_id, data={
            "id": course_id, "slug": course_id, "title": "NE 101 Test",
            "instructor_id": "@ben:ut",
            "manifest": {
                "course_id": course_id, "title": "NE 101 Test",
                "version": "1.0.0",
            },
            "system_prompt": "Be concise.",
        })
        reg.register(kind="classroom", name=classroom_id, data={
            "id": classroom_id, "slug": classroom_id, "title": "NE 101",
            "instructor_id": "@ben:ut",
            "course_id": course_id, "course_slug": course_id,
            "course_version": "1.0.0",
            "course_system_prompt": "Be concise.",
            "steps": [asdict(s) for s in checklist.steps],
        })

        # Upload reactor-physics materials to the coordinator-side store.
        coord_dir = (
            tmp_path / ".axi" / "coordinator"
            / "classrooms" / classroom_id
        )
        materials = ClassroomMaterialsStore(coord_dir)
        materials.add_text(
            "Criticality is the state where a nuclear chain reaction "
            "is self-sustaining.",
            filename="criticality.txt", title="Criticality",
        )
        materials.add_text(
            "Control rods absorb neutrons to regulate reactor power.",
            filename="control_rods.txt", title="Control Rods",
        )
        return classroom_id

    def test_uses_uploaded_materials_not_demo_corpus(
        self, tmp_path, monkeypatch,
    ):
        classroom_id = self._make_real_classroom(tmp_path, monkeypatch)
        result = enhanced_dry_run(
            classroom_id=classroom_id,
            queries=["What is criticality?"],
        )
        assert "error" not in result
        retrieved = result["transcript"][0].get("retrieved", [])
        titles = {d.get("title") for d in retrieved}
        # Must hit the actual reactor docs, never the demo's Newton docs.
        assert "Criticality" in titles
        assert not any(
            "Newton" in (t or "") or "Momentum" in (t or "") for t in titles
        )

    def test_default_queries_derived_from_corpus(
        self, tmp_path, monkeypatch,
    ):
        classroom_id = self._make_real_classroom(tmp_path, monkeypatch)
        result = enhanced_dry_run(classroom_id=classroom_id)
        assert "error" not in result
        queries = [t.get("query", "") for t in result["transcript"]]
        # Without --query, defaults must come from the uploaded titles,
        # not the hard-coded Newton's-law strings.
        joined = " ".join(queries).lower()
        assert "criticality" in joined or "control" in joined
        assert "newton" not in joined
        assert "work-energy" not in joined


# ---------------------------------------------------------------------------
# CLI — publish + dry-run-enhanced
# ---------------------------------------------------------------------------


class TestPublishCLI:
    def test_publish_command_happy_path(self, demo_classroom, capsys):
        import json

        from axiom.extensions.builtins.classroom.cli import main

        rc = main(
            [
                "publish", demo_classroom,
                "--approver", "@ben:ut",
                "--json",
            ]
        )
        assert rc == 0
        data = json.loads(capsys.readouterr().out)
        assert data["published"] is True
        assert data["state"] == PUBLISHED

    def test_publish_rejects_unpublishable_classroom(self, capsys):
        import json

        from axiom.extensions.builtins.classroom.cli import main

        rc = main(
            ["publish", "nonexistent", "--approver", "@ben:ut", "--json"]
        )
        assert rc == 1
        data = json.loads(capsys.readouterr().out)
        assert data["published"] is False


class TestDryRunEnhancedCLI:
    def test_uses_course_corpus(self, demo_classroom, capsys):
        import json

        from axiom.extensions.builtins.classroom.cli import main

        rc = main(
            [
                "prep", "dry-run-enhanced", demo_classroom,
                "--query", "Newton's second law",
                "--json",
            ]
        )
        assert rc == 0
        data = json.loads(capsys.readouterr().out)
        assert data["turns"] == 1


# ---------------------------------------------------------------------------
# Chat tools — classroom_publish + classroom_prep_dry_run_enhanced
# ---------------------------------------------------------------------------


class TestPublishChatTools:
    def test_both_tools_registered(self):
        from axiom.extensions.builtins.classroom.chat_tools import prep_tools

        names = {t.name for t in prep_tools.TOOLS}
        assert "classroom_publish" in names
        assert "classroom_prep_dry_run_enhanced" in names

    def test_publish_chat_tool_happy(self, demo_classroom):
        from axiom.extensions.builtins.classroom.chat_tools import prep_tools

        result = prep_tools.execute(
            "classroom_publish",
            {"classroom_id": demo_classroom, "approver": "@ben:ut"},
        )
        assert result["published"] is True

    def test_publish_chat_tool_missing_params(self):
        from axiom.extensions.builtins.classroom.chat_tools import prep_tools

        result = prep_tools.execute("classroom_publish", {})
        assert result["published"] is False

    def test_dry_run_enhanced_chat_tool(self, demo_classroom):
        from axiom.extensions.builtins.classroom.chat_tools import prep_tools

        result = prep_tools.execute(
            "classroom_prep_dry_run_enhanced",
            {
                "classroom_id": demo_classroom,
                "queries": ["What is Newton's second law?"],
            },
        )
        assert result["turns"] == 1

    def test_dry_run_enhanced_defaults(self, demo_classroom):
        from axiom.extensions.builtins.classroom.chat_tools import prep_tools

        result = prep_tools.execute(
            "classroom_prep_dry_run_enhanced", {"classroom_id": demo_classroom},
        )
        assert result["turns"] >= 2
