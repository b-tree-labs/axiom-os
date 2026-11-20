# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tests for wrap template — FW-4 P5.

Produces a proposed updated CourseManifest plus a rationale derived
from the cohort's grade distributions. Instructor reviews the
proposal; nothing is auto-applied. This is intentionally advisory
only in v0.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from axiom.extensions.builtins.classroom.conclusion import (
    propose_template_update,
)


@pytest.fixture(autouse=True)
def _isolated_runtime(tmp_path, monkeypatch):
    monkeypatch.setenv("AXIOM_RUNTIME_ROOT", str(tmp_path))
    import axiom.extensions.builtins.classroom.operational_store as store

    store._registry = None
    yield tmp_path
    store._registry = None


def _seed_demo_with_ledger(
    runtime_root: Path, ledger_entries: list[dict] | None = None,
) -> str:
    from axiom.extensions.builtins.classroom.demo import (
        DEMO_CLASSROOM_ID,
        seed_demo,
    )

    seed_demo()
    if ledger_entries:
        gdir = runtime_root / "classrooms" / DEMO_CLASSROOM_ID / "grades"
        gdir.mkdir(parents=True, exist_ok=True)
        for entry in ledger_entries:
            (gdir / f"{entry['assessment_id']}.json").write_text(
                json.dumps(entry)
            )
    return DEMO_CLASSROOM_ID


# ---------------------------------------------------------------------------
# propose_template_update
# ---------------------------------------------------------------------------


class TestProposeTemplateUpdate:
    def test_returns_proposed_manifest_and_rationale(
        self, _isolated_runtime,
    ):
        classroom_id = _seed_demo_with_ledger(
            _isolated_runtime,
            [
                {
                    "classroom_id": "demo-classical-mechanics-spring",
                    "assessment_id": "baseline",
                    "grades": [{"student_id": "s-a", "score": 0.85}],
                },
            ],
        )
        result = propose_template_update(classroom_id=classroom_id)
        assert "proposed_manifest" in result
        assert "rationale" in result
        assert isinstance(result["rationale"], list)

    def test_flags_low_mean_assessment(self, _isolated_runtime):
        """An assessment with mean < 0.7 → suggest a pre-checkpoint rail."""
        classroom_id = _seed_demo_with_ledger(
            _isolated_runtime,
            [
                {
                    "classroom_id": "demo-classical-mechanics-spring",
                    "assessment_id": "baseline",
                    "grades": [
                        {"student_id": "s-a", "score": 0.50},
                        {"student_id": "s-b", "score": 0.55},
                        {"student_id": "s-c", "score": 0.60},
                    ],
                },
            ],
        )
        result = propose_template_update(classroom_id=classroom_id)
        hits = [
            r for r in result["rationale"]
            if r.get("assessment_id") == "baseline"
            and r.get("signal") == "low_mean"
        ]
        assert hits

    def test_flags_high_variance_assessment(self, _isolated_runtime):
        """An assessment with stdev > 0.2 → suggest rubric review."""
        classroom_id = _seed_demo_with_ledger(
            _isolated_runtime,
            [
                {
                    "classroom_id": "demo-classical-mechanics-spring",
                    "assessment_id": "midpoint",
                    "grades": [
                        {"student_id": "s-a", "score": 0.30},
                        {"student_id": "s-b", "score": 0.95},
                        {"student_id": "s-c", "score": 0.40},
                        {"student_id": "s-d", "score": 0.90},
                    ],
                },
            ],
        )
        result = propose_template_update(classroom_id=classroom_id)
        signals = {
            (r.get("assessment_id"), r.get("signal"))
            for r in result["rationale"]
        }
        assert ("midpoint", "high_variance") in signals

    def test_adds_retake_checkpoint_when_students_fail(
        self, _isolated_runtime,
    ):
        """Failed students (score < 0.6) → propose a retake checkpoint."""
        classroom_id = _seed_demo_with_ledger(
            _isolated_runtime,
            [
                {
                    "classroom_id": "demo-classical-mechanics-spring",
                    "assessment_id": "midpoint",
                    "grades": [
                        {"student_id": "s-a", "score": 0.40},
                        {"student_id": "s-b", "score": 0.50},
                        {"student_id": "s-c", "score": 0.90},
                    ],
                },
            ],
        )
        result = propose_template_update(classroom_id=classroom_id)
        manifest = result["proposed_manifest"]
        ck_ids = [cp["id"] for cp in manifest.get("checkpoints") or []]
        assert any("retake" in cid.lower() for cid in ck_ids)

    def test_quiet_on_healthy_cohort(self, _isolated_runtime):
        """All assessments at solid means + low variance → no complaints,
        manifest mirrors the existing one."""
        classroom_id = _seed_demo_with_ledger(
            _isolated_runtime,
            [
                {
                    "classroom_id": "demo-classical-mechanics-spring",
                    "assessment_id": "baseline",
                    "grades": [
                        {"student_id": "s-a", "score": 0.88},
                        {"student_id": "s-b", "score": 0.92},
                    ],
                },
                {
                    "classroom_id": "demo-classical-mechanics-spring",
                    "assessment_id": "midpoint",
                    "grades": [
                        {"student_id": "s-a", "score": 0.87},
                        {"student_id": "s-b", "score": 0.90},
                    ],
                },
            ],
        )
        result = propose_template_update(classroom_id=classroom_id)
        assert all(
            r.get("signal") not in ("low_mean", "high_variance")
            for r in result["rationale"]
        )

    def test_unknown_classroom_returns_error(self):
        result = propose_template_update(classroom_id="nope")
        assert "error" in result


# ---------------------------------------------------------------------------
# CLI — axi classroom wrap template
# ---------------------------------------------------------------------------


class TestTemplateCLI:
    def test_writes_yaml_file(self, _isolated_runtime, tmp_path, capsys):
        classroom_id = _seed_demo_with_ledger(_isolated_runtime)
        out = tmp_path / "proposed.yaml"

        from axiom.extensions.builtins.classroom.cli import main

        rc = main(
            [
                "wrap", "template", classroom_id,
                "--out", str(out),
                "--json",
            ]
        )
        assert rc == 0
        assert out.exists()
        loaded = yaml.safe_load(out.read_text())
        assert loaded.get("id")

    def test_without_out_prints_to_stdout(
        self, _isolated_runtime, capsys,
    ):
        classroom_id = _seed_demo_with_ledger(_isolated_runtime)
        from axiom.extensions.builtins.classroom.cli import main

        rc = main(["wrap", "template", classroom_id, "--json"])
        assert rc == 0
        data = json.loads(capsys.readouterr().out)
        assert "proposed_manifest" in data


# ---------------------------------------------------------------------------
# Chat tool — classroom_wrap_template
# ---------------------------------------------------------------------------


class TestTemplateChatTool:
    def test_tool_registered(self):
        from axiom.extensions.builtins.classroom.chat_tools import prep_tools

        names = {t.name for t in prep_tools.TOOLS}
        assert "classroom_wrap_template" in names

    def test_tool_returns_proposal(self, _isolated_runtime):
        classroom_id = _seed_demo_with_ledger(_isolated_runtime)
        from axiom.extensions.builtins.classroom.chat_tools import prep_tools

        result = prep_tools.execute(
            "classroom_wrap_template", {"classroom_id": classroom_id},
        )
        assert "proposed_manifest" in result
