# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tests for wrap harvest — FW-4 P3.

Harvest bundles a classroom's cohort data into a ``.axiompack`` zip
for research export. Principal ids are anonymized deterministically
via the existing ``medallion.export.pseudonymize`` helper so
longitudinal joins across bundles still work.
"""

from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest

from axiom.extensions.builtins.classroom.conclusion import harvest_classroom


@pytest.fixture(autouse=True)
def _isolated_runtime(tmp_path, monkeypatch):
    monkeypatch.setenv("AXIOM_RUNTIME_ROOT", str(tmp_path))
    import axiom.extensions.builtins.classroom.operational_store as store

    store._registry = None
    yield tmp_path
    store._registry = None


@pytest.fixture
def demo_classroom(_isolated_runtime):
    from axiom.extensions.builtins.classroom.demo import (
        DEMO_CLASSROOM_ID,
        seed_demo,
    )

    seed_demo()
    return DEMO_CLASSROOM_ID


@pytest.fixture
def demo_with_grades(demo_classroom, _isolated_runtime):
    """Classroom with a populated grade ledger."""
    grades_dir = (
        _isolated_runtime / "classrooms" / demo_classroom / "grades"
    )
    grades_dir.mkdir(parents=True, exist_ok=True)
    (grades_dir / "baseline.json").write_text(
        json.dumps(
            {
                "classroom_id": demo_classroom,
                "assessment_id": "baseline",
                "grades": [
                    {"student_id": "s-alice", "score": 0.85},
                    {"student_id": "s-bob", "score": 0.70},
                ],
            }
        )
    )
    return demo_classroom


# ---------------------------------------------------------------------------
# harvest_classroom
# ---------------------------------------------------------------------------


class TestHarvestClassroom:
    def test_writes_axiompack_at_out_path(self, demo_classroom, tmp_path):
        out = tmp_path / "harvest.axiompack"
        result = harvest_classroom(classroom_id=demo_classroom, out_path=out)
        assert result["harvested"] is True
        assert Path(result["path"]).exists()
        assert Path(result["path"]) == out

    def test_pack_contents_include_core_files(self, demo_classroom, tmp_path):
        out = tmp_path / "harvest.axiompack"
        harvest_classroom(classroom_id=demo_classroom, out_path=out)
        with zipfile.ZipFile(out) as zf:
            names = set(zf.namelist())
        required = {"MANIFEST.yaml", "course.json", "classroom.json", "README.md"}
        assert required.issubset(names)

    def test_grades_file_present_when_ledger_exists(
        self, demo_with_grades, tmp_path,
    ):
        out = tmp_path / "harvest.axiompack"
        harvest_classroom(classroom_id=demo_with_grades, out_path=out)
        with zipfile.ZipFile(out) as zf:
            names = set(zf.namelist())
            assert "grades.jsonl" in names
            grades_text = zf.read("grades.jsonl").decode("utf-8")
        # One JSONL row per graded student
        lines = [ln for ln in grades_text.splitlines() if ln.strip()]
        assert len(lines) == 2

    def test_grades_file_absent_when_no_ledger(self, demo_classroom, tmp_path):
        out = tmp_path / "harvest.axiompack"
        harvest_classroom(classroom_id=demo_classroom, out_path=out)
        with zipfile.ZipFile(out) as zf:
            # Either absent, or present-but-empty is acceptable
            names = zf.namelist()
            if "grades.jsonl" in names:
                assert not zf.read("grades.jsonl").strip()

    def test_pseudonymizes_student_ids(self, demo_with_grades, tmp_path):
        from axiom.medallion.export import pseudonymize

        out = tmp_path / "harvest.axiompack"
        harvest_classroom(classroom_id=demo_with_grades, out_path=out)
        with zipfile.ZipFile(out) as zf:
            grades_text = zf.read("grades.jsonl").decode("utf-8")

        # Raw student ids must not appear in the harvested grades.
        assert "s-alice" not in grades_text
        assert "s-bob" not in grades_text
        # The deterministic pseudonym for "s-alice" must appear instead.
        assert pseudonymize("s-alice") in grades_text

    def test_roster_anonymized_in_classroom_json(
        self, demo_classroom, tmp_path,
    ):
        out = tmp_path / "harvest.axiompack"
        harvest_classroom(classroom_id=demo_classroom, out_path=out)
        with zipfile.ZipFile(out) as zf:
            classroom_json = json.loads(zf.read("classroom.json"))

        roster = classroom_json.get("lms_roster") or []
        # Email + name must be redacted; id must be pseudonymized.
        for s in roster:
            assert s.get("email") in ("", None, "<redacted>")
            assert s.get("name") in ("", None, "<redacted>")
            # Pseudonymized id is non-empty hex-ish
            assert s.get("id")
            assert "@" not in s.get("id", "")  # no email-shaped ids

    def test_unknown_classroom_returns_error(self, tmp_path):
        out = tmp_path / "harvest.axiompack"
        result = harvest_classroom(classroom_id="nope", out_path=out)
        assert result["harvested"] is False
        assert "error" in result
        assert not out.exists()

    def test_manifest_yaml_lists_bundle_contents(
        self, demo_with_grades, tmp_path,
    ):
        out = tmp_path / "harvest.axiompack"
        harvest_classroom(classroom_id=demo_with_grades, out_path=out)
        with zipfile.ZipFile(out) as zf:
            mf = zf.read("MANIFEST.yaml").decode("utf-8")
        # MANIFEST lists classroom id, timestamp, and pseudonymization note
        assert demo_with_grades in mf
        assert "pseudonym" in mf.lower() or "anonymiz" in mf.lower()


# ---------------------------------------------------------------------------
# CLI — axi classroom wrap harvest
# ---------------------------------------------------------------------------


class TestHarvestCLI:
    def test_cli_creates_pack(self, demo_classroom, tmp_path, capsys):
        from axiom.extensions.builtins.classroom.cli import main

        out = tmp_path / "harvest.axiompack"
        rc = main(
            [
                "wrap", "harvest", demo_classroom,
                "--out", str(out),
                "--json",
            ]
        )
        assert rc == 0
        assert out.exists()

    def test_cli_rejects_unknown_classroom(self, tmp_path, capsys):
        from axiom.extensions.builtins.classroom.cli import main

        out = tmp_path / "harvest.axiompack"
        rc = main(
            ["wrap", "harvest", "nope", "--out", str(out), "--json"]
        )
        assert rc == 1
        assert not out.exists()


# ---------------------------------------------------------------------------
# Chat tool — classroom_wrap_harvest
# ---------------------------------------------------------------------------


class TestHarvestChatTool:
    def test_tool_registered(self):
        from axiom.extensions.builtins.classroom.chat_tools import prep_tools

        names = {t.name for t in prep_tools.TOOLS}
        assert "classroom_wrap_harvest" in names

    def test_tool_creates_pack(self, demo_classroom, tmp_path):
        from axiom.extensions.builtins.classroom.chat_tools import prep_tools

        out = tmp_path / "harvest.axiompack"
        result = prep_tools.execute(
            "classroom_wrap_harvest",
            {"classroom_id": demo_classroom, "out_path": str(out)},
        )
        assert result["harvested"] is True
        assert out.exists()

    def test_tool_missing_params_error(self):
        from axiom.extensions.builtins.classroom.chat_tools import prep_tools

        result = prep_tools.execute("classroom_wrap_harvest", {})
        assert result.get("harvested") is False
