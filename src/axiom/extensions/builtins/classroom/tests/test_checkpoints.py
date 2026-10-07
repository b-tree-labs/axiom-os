# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tests for checkpoint configuration — FW-1 P3a.

Course checkpoints formalize Baseline / Midpoint / Final milestones
per project_course_checkpoints memory. Defaults ship with every course;
instructors can add, remove, or skip defaults entirely. Timing accepts
keyword ("enrollment_complete", "midway", "course_end", "course_start")
OR ISO-8601 date string.
"""

from __future__ import annotations

import pytest

from axiom.extensions.builtins.classroom.checkpoints import (
    DEFAULT_CHECKPOINTS,
    KEYWORD_TIMINGS,
    apply_default_checkpoints,
    parse_checkpoint,
    validate_checkpoint_timing,
)

# ---------------------------------------------------------------------------
# validate_checkpoint_timing
# ---------------------------------------------------------------------------


class TestTimingValidation:
    @pytest.mark.parametrize(
        "keyword",
        ["enrollment_complete", "course_start", "midway", "course_end"],
    )
    def test_accepts_keyword(self, keyword):
        assert validate_checkpoint_timing(keyword) is True

    @pytest.mark.parametrize(
        "iso",
        [
            "2026-07-15",
            "2026-07-15T09:00:00",
            "2026-07-15T09:00:00-05:00",
            "2026-07-15T14:00:00Z",
        ],
    )
    def test_accepts_iso_date(self, iso):
        assert validate_checkpoint_timing(iso) is True

    @pytest.mark.parametrize(
        "bad",
        [
            "",
            "someday",
            "07/15/2026",  # US format — not ISO
            "next week",
            "2026-13-01",  # month 13 is invalid
            None,
        ],
    )
    def test_rejects_bad_timing(self, bad):
        assert validate_checkpoint_timing(bad) is False

    def test_keywords_constant_is_frozenset(self):
        assert isinstance(KEYWORD_TIMINGS, frozenset)
        assert "enrollment_complete" in KEYWORD_TIMINGS


# ---------------------------------------------------------------------------
# DEFAULT_CHECKPOINTS shape
# ---------------------------------------------------------------------------


class TestDefaults:
    def test_ships_three_defaults(self):
        assert len(DEFAULT_CHECKPOINTS) == 3
        ids = [c["id"] for c in DEFAULT_CHECKPOINTS]
        assert ids == ["baseline", "midpoint", "final"]

    def test_defaults_use_keyword_timings(self):
        for cp in DEFAULT_CHECKPOINTS:
            assert cp["timing"] in KEYWORD_TIMINGS

    def test_defaults_are_quiz_method(self):
        """Quiz is the default method — can be overridden per instructor."""
        for cp in DEFAULT_CHECKPOINTS:
            assert cp["method"] == "quiz"

    def test_defaults_required_by_default(self):
        """Instructors opt out with required=False or skip-defaults."""
        for cp in DEFAULT_CHECKPOINTS:
            assert cp["required"] is True


# ---------------------------------------------------------------------------
# Checkpoint dataclass
# ---------------------------------------------------------------------------


class TestCheckpointParsing:
    def test_parse_complete_dict(self):
        cp = parse_checkpoint(
            {
                "id": "baseline",
                "label": "Baseline",
                "timing": "enrollment_complete",
                "method": "quiz",
                "questionnaire_id": "pre-quiz",
                "required": True,
            }
        )
        assert cp.id == "baseline"
        assert cp.label == "Baseline"
        assert cp.timing == "enrollment_complete"
        assert cp.method == "quiz"
        assert cp.questionnaire_id == "pre-quiz"
        assert cp.required is True

    def test_parse_applies_sensible_defaults(self):
        """Just `id` is enough — label defaults to id, method defaults to 'quiz'."""
        cp = parse_checkpoint({"id": "retake"})
        assert cp.id == "retake"
        assert cp.label == "retake"
        assert cp.method == "quiz"
        assert cp.required is False  # non-default checkpoints default to optional

    def test_parse_rejects_missing_id(self):
        with pytest.raises(ValueError, match="id"):
            parse_checkpoint({"label": "no id"})

    def test_parse_rejects_bad_timing(self):
        with pytest.raises(ValueError, match="timing"):
            parse_checkpoint({"id": "x", "timing": "someday"})

    def test_parse_rejects_bad_method(self):
        with pytest.raises(ValueError, match="method"):
            parse_checkpoint({"id": "x", "method": "telepathy"})

    def test_parse_roundtrips_through_to_dict(self):
        original = {
            "id": "baseline",
            "label": "Baseline",
            "timing": "enrollment_complete",
            "method": "quiz",
            "questionnaire_id": "pre-quiz",
            "required": True,
        }
        cp = parse_checkpoint(original)
        assert cp.to_dict() == original


# ---------------------------------------------------------------------------
# apply_default_checkpoints
# ---------------------------------------------------------------------------


class TestApplyDefaults:
    def test_injects_defaults_when_absent(self):
        manifest: dict = {"id": "c1", "title": "T", "version": "1"}
        apply_default_checkpoints(manifest)
        assert "checkpoints" in manifest
        assert len(manifest["checkpoints"]) == 3

    def test_preserves_explicit_checkpoints(self):
        manifest: dict = {
            "id": "c1",
            "title": "T",
            "version": "1",
            "checkpoints": [
                {"id": "custom-1", "timing": "midway", "method": "portfolio"},
            ],
        }
        apply_default_checkpoints(manifest)
        assert len(manifest["checkpoints"]) == 1
        assert manifest["checkpoints"][0]["id"] == "custom-1"

    def test_preserves_empty_list_opt_out(self):
        """Empty list is an explicit opt-out — don't re-inject defaults."""
        manifest: dict = {
            "id": "c1", "title": "T", "version": "1", "checkpoints": [],
        }
        apply_default_checkpoints(manifest)
        assert manifest["checkpoints"] == []


# ---------------------------------------------------------------------------
# CLI — axi classroom prep checkpoints {list|add|remove|skip-defaults}
# ---------------------------------------------------------------------------


class TestCheckpointsCLI:
    @pytest.fixture(autouse=True)
    def _isolated_runtime(self, tmp_path, monkeypatch):
        monkeypatch.setenv("AXIOM_RUNTIME_ROOT", str(tmp_path))
        import axiom.extensions.builtins.classroom.operational_store as store

        store._registry = None
        yield
        store._registry = None

    @pytest.fixture
    def seeded_course(self):
        from axiom.extensions.builtins.classroom.demo import clone_demo_course, seed_demo

        seed_demo()
        clone_demo_course(new_course_id="my-course", instructor_id="@ben:ut")
        return "my-course"

    def test_list_shows_defaults_after_clone(self, seeded_course, capsys):
        from axiom.extensions.builtins.classroom.cli import main

        rc = main(["prep", "checkpoints", "list", seeded_course, "--json"])
        assert rc == 0
        import json
        data = json.loads(capsys.readouterr().out)
        # Demo course ships with the default 3; cloning preserves them.
        assert data["count"] == 3
        ids = [cp["id"] for cp in data["checkpoints"]]
        assert ids == ["baseline", "midpoint", "final"]

    def test_add_with_keyword_timing(self, seeded_course, capsys):
        from axiom.extensions.builtins.classroom.cli import main

        rc = main(
            [
                "prep", "checkpoints", "add", seeded_course,
                "--id", "retake", "--timing", "course_end",
                "--method", "quiz", "--json",
            ]
        )
        assert rc == 0
        import json
        data = json.loads(capsys.readouterr().out)
        assert data["added"]["id"] == "retake"

    def test_add_with_iso_timing(self, seeded_course):
        from axiom.extensions.builtins.classroom.cli import main

        rc = main(
            [
                "prep", "checkpoints", "add", seeded_course,
                "--id", "mid-term-1", "--timing", "2026-07-15",
                "--json",
            ]
        )
        assert rc == 0

    def test_add_rejects_bad_timing(self, seeded_course, capsys):
        from axiom.extensions.builtins.classroom.cli import main

        rc = main(
            [
                "prep", "checkpoints", "add", seeded_course,
                "--id", "bad", "--timing", "someday", "--json",
            ]
        )
        assert rc == 1
        import json
        data = json.loads(capsys.readouterr().out)
        assert "timing" in data["error"]

    def test_remove_checkpoint(self, seeded_course):
        from axiom.extensions.builtins.classroom.cli import main
        from axiom.extensions.builtins.classroom.operational_store import (
            load_course_data,
        )

        rc = main(
            ["prep", "checkpoints", "remove", seeded_course,
             "--id", "midpoint", "--json"]
        )
        assert rc == 0
        data = load_course_data(seeded_course)
        ids = [cp["id"] for cp in data["manifest"].get("checkpoints", [])]
        assert "midpoint" not in ids

    def test_skip_defaults_empties_list(self, seeded_course):
        from axiom.extensions.builtins.classroom.cli import main
        from axiom.extensions.builtins.classroom.operational_store import (
            load_course_data,
        )

        rc = main(
            ["prep", "checkpoints", "skip-defaults", seeded_course, "--json"]
        )
        assert rc == 0
        data = load_course_data(seeded_course)
        assert data["manifest"].get("checkpoints") == []


# ---------------------------------------------------------------------------
# Chat tool — classroom_prep_configure_checkpoints
# ---------------------------------------------------------------------------


class TestCheckpointChatTool:
    @pytest.fixture(autouse=True)
    def _isolated_runtime(self, tmp_path, monkeypatch):
        monkeypatch.setenv("AXIOM_RUNTIME_ROOT", str(tmp_path))
        import axiom.extensions.builtins.classroom.operational_store as store

        store._registry = None
        yield
        store._registry = None

    @pytest.fixture
    def seeded_course(self):
        from axiom.extensions.builtins.classroom.demo import clone_demo_course, seed_demo

        seed_demo()
        clone_demo_course(new_course_id="my-course", instructor_id="@ben:ut")
        return "my-course"

    def test_tool_registered(self):
        from axiom.extensions.builtins.classroom.chat_tools import prep_tools

        names = {t.name for t in prep_tools.TOOLS}
        assert "classroom_prep_configure_checkpoints" in names

    def test_list_action(self, seeded_course):
        from axiom.extensions.builtins.classroom.chat_tools import prep_tools

        result = prep_tools.execute(
            "classroom_prep_configure_checkpoints",
            {"action": "list", "course_id": seeded_course},
        )
        assert result["count"] == 3
        assert [cp["id"] for cp in result["checkpoints"]] == [
            "baseline", "midpoint", "final",
        ]

    def test_add_action(self, seeded_course):
        from axiom.extensions.builtins.classroom.chat_tools import prep_tools

        result = prep_tools.execute(
            "classroom_prep_configure_checkpoints",
            {
                "action": "add",
                "course_id": seeded_course,
                "checkpoint_id": "retake",
                "timing": "course_end",
            },
        )
        assert result["added"]["id"] == "retake"

    def test_remove_action(self, seeded_course):
        from axiom.extensions.builtins.classroom.chat_tools import prep_tools

        result = prep_tools.execute(
            "classroom_prep_configure_checkpoints",
            {
                "action": "remove",
                "course_id": seeded_course,
                "checkpoint_id": "midpoint",
            },
        )
        assert "midpoint" not in [cp["id"] for cp in result["checkpoints"]]

    def test_skip_defaults_action(self, seeded_course):
        from axiom.extensions.builtins.classroom.chat_tools import prep_tools

        result = prep_tools.execute(
            "classroom_prep_configure_checkpoints",
            {"action": "skip-defaults", "course_id": seeded_course},
        )
        assert result["count"] == 0

    def test_bad_action_error(self, seeded_course):
        from axiom.extensions.builtins.classroom.chat_tools import prep_tools

        result = prep_tools.execute(
            "classroom_prep_configure_checkpoints",
            {"action": "demolish", "course_id": seeded_course},
        )
        assert "error" in result

    def test_missing_course_error(self):
        from axiom.extensions.builtins.classroom.chat_tools import prep_tools

        result = prep_tools.execute(
            "classroom_prep_configure_checkpoints",
            {"action": "list", "course_id": "nope"},
        )
        assert "error" in result
