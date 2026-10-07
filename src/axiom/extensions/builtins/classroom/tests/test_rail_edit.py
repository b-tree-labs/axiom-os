# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tests for rail edit flow — Track 5.

Two paths:
- CLI: launches ``$EDITOR`` on a temp YAML file, reads the saved text,
  validates, persists. Tested by monkey-patching the editor launch.
- Chat tool / Python API: accepts the edited YAML text as a string —
  no editor involved. Used by AXI + non-interactive tooling.
"""

from __future__ import annotations

import json

import pytest
import yaml

from axiom.extensions.builtins.classroom.rail_edit import (
    apply_rail_edit,
    load_rail_for_edit,
)


@pytest.fixture(autouse=True)
def _isolated_runtime(tmp_path, monkeypatch):
    monkeypatch.setenv("AXIOM_RUNTIME_ROOT", str(tmp_path))
    import axiom.extensions.builtins.classroom.operational_store as store

    store._registry = None
    yield tmp_path
    store._registry = None


@pytest.fixture
def course_with_rail():
    from axiom.extensions.builtins.classroom.demo import (
        clone_demo_course,
        seed_demo,
    )
    from axiom.extensions.builtins.classroom.operational_store import (
        _reg,
        load_course_data,
    )
    from axiom.extensions.builtins.classroom.question_banks import (
        add_rail_from_bank,
    )

    seed_demo()
    clone_demo_course(new_course_id="my-course", instructor_id="@ben:ut")
    # Add a rail explicitly so edit has a target.
    data = load_course_data("my-course")
    manifest = dict(data.get("manifest") or {})
    add_rail_from_bank(manifest, rail_id="intake", bank_id="axiom-core-starter")
    updated = dict(data)
    updated["manifest"] = manifest
    updated["rails"] = list(manifest.get("rails") or [])
    _reg().register(kind="course", name="my-course", data=updated)
    return "my-course"


# ---------------------------------------------------------------------------
# load_rail_for_edit
# ---------------------------------------------------------------------------


class TestLoadRailForEdit:
    def test_returns_yaml_text(self, course_with_rail):
        text = load_rail_for_edit(course_id=course_with_rail, rail_id="intake")
        loaded = yaml.safe_load(text)
        assert loaded["id"] == "intake"
        assert loaded["source"] == "axiom-core-starter"
        assert len(loaded["questions"]) >= 3

    def test_unknown_course(self):
        with pytest.raises(ValueError, match="course"):
            load_rail_for_edit(course_id="nope", rail_id="x")

    def test_unknown_rail(self, course_with_rail):
        with pytest.raises(ValueError, match="rail"):
            load_rail_for_edit(
                course_id=course_with_rail, rail_id="nonexistent",
            )


# ---------------------------------------------------------------------------
# apply_rail_edit
# ---------------------------------------------------------------------------


class TestApplyRailEdit:
    def test_applies_valid_edit(self, course_with_rail):
        original = load_rail_for_edit(
            course_id=course_with_rail, rail_id="intake",
        )
        edited = yaml.safe_load(original)
        edited["required"] = False
        edited["questions"].append(
            {
                "id": "custom-q",
                "prompt": "Have you completed the prereq?",
                "response_type": "yes_no",
            }
        )
        text = yaml.safe_dump(edited)
        result = apply_rail_edit(
            course_id=course_with_rail, rail_id="intake", new_yaml=text,
        )
        assert result["applied"] is True

        from axiom.extensions.builtins.classroom.operational_store import (
            load_course_data,
        )
        data = load_course_data(course_with_rail)
        rail = next(
            r for r in data["manifest"]["rails"] if r["id"] == "intake"
        )
        assert rail["required"] is False
        assert any(q["id"] == "custom-q" for q in rail["questions"])

    def test_rejects_id_change(self, course_with_rail):
        """Editing the id of a rail is ambiguous; force the user to
        remove + add instead."""
        original_yaml = load_rail_for_edit(
            course_id=course_with_rail, rail_id="intake",
        )
        edited = yaml.safe_load(original_yaml)
        edited["id"] = "intake-renamed"
        result = apply_rail_edit(
            course_id=course_with_rail,
            rail_id="intake",
            new_yaml=yaml.safe_dump(edited),
        )
        assert result["applied"] is False
        assert "id" in result.get("error", "").lower()

    def test_rejects_invalid_yaml(self, course_with_rail):
        result = apply_rail_edit(
            course_id=course_with_rail,
            rail_id="intake",
            new_yaml="not: valid: yaml: {{}",
        )
        assert result["applied"] is False
        assert "yaml" in result.get("error", "").lower()

    def test_rejects_missing_required_field(self, course_with_rail):
        result = apply_rail_edit(
            course_id=course_with_rail,
            rail_id="intake",
            new_yaml=yaml.safe_dump({"questions": []}),  # no 'id' or 'source'
        )
        assert result["applied"] is False

    def test_unknown_rail(self, course_with_rail):
        result = apply_rail_edit(
            course_id=course_with_rail,
            rail_id="nonexistent",
            new_yaml=yaml.safe_dump(
                {"id": "x", "source": "y", "questions": []},
            ),
        )
        assert result["applied"] is False


# ---------------------------------------------------------------------------
# CLI — axi classroom prep rails edit
# ---------------------------------------------------------------------------


class TestRailsEditCLI:
    def test_editor_invocation_happy_path(
        self, course_with_rail, tmp_path, monkeypatch, capsys,
    ):
        """Simulate $EDITOR by patching the launcher to overwrite the
        temp file with an edited YAML."""

        def _fake_editor(path):
            current = yaml.safe_load(open(path).read())
            current["required"] = False
            open(path, "w").write(yaml.safe_dump(current))
            return 0

        import axiom.extensions.builtins.classroom.rail_edit as mod

        monkeypatch.setattr(mod, "_launch_editor", _fake_editor)

        from axiom.extensions.builtins.classroom.cli import main

        rc = main(
            [
                "prep", "rails", "edit", course_with_rail,
                "--rail-id", "intake",
                "--json",
            ]
        )
        assert rc == 0
        data = json.loads(capsys.readouterr().out)
        assert data["applied"] is True

    def test_editor_unchanged_is_noop(
        self, course_with_rail, monkeypatch, capsys,
    ):
        """If the instructor closes the editor without saving changes,
        apply should still succeed (no-op persistence)."""

        def _noop_editor(path):
            return 0

        import axiom.extensions.builtins.classroom.rail_edit as mod

        monkeypatch.setattr(mod, "_launch_editor", _noop_editor)

        from axiom.extensions.builtins.classroom.cli import main

        rc = main(
            [
                "prep", "rails", "edit", course_with_rail,
                "--rail-id", "intake",
                "--json",
            ]
        )
        assert rc == 0


# ---------------------------------------------------------------------------
# Chat tool — classroom_prep_edit_rail (no editor; text in / text out)
# ---------------------------------------------------------------------------


class TestEditRailChatTool:
    def test_tool_registered(self):
        from axiom.extensions.builtins.classroom.chat_tools import prep_tools

        names = {t.name for t in prep_tools.TOOLS}
        assert "classroom_prep_edit_rail" in names

    def test_tool_returns_current_yaml_when_no_new_yaml(self, course_with_rail):
        from axiom.extensions.builtins.classroom.chat_tools import prep_tools

        result = prep_tools.execute(
            "classroom_prep_edit_rail",
            {"course_id": course_with_rail, "rail_id": "intake"},
        )
        assert result["current_yaml"]
        assert "intake" in result["current_yaml"]

    def test_tool_applies_new_yaml(self, course_with_rail):
        from axiom.extensions.builtins.classroom.chat_tools import prep_tools

        current_yaml = prep_tools.execute(
            "classroom_prep_edit_rail",
            {"course_id": course_with_rail, "rail_id": "intake"},
        )["current_yaml"]
        edited = yaml.safe_load(current_yaml)
        edited["required"] = False
        result = prep_tools.execute(
            "classroom_prep_edit_rail",
            {
                "course_id": course_with_rail,
                "rail_id": "intake",
                "new_yaml": yaml.safe_dump(edited),
            },
        )
        assert result["applied"] is True
