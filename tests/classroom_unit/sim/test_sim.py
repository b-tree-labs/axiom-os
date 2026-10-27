# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""TDD tests for the Prague simulator + rubric (#68, #69)."""

from __future__ import annotations

import json

# ---------------------------------------------------------------------------
# SimStudent
# ---------------------------------------------------------------------------


class TestSimStudent:
    def test_classroom_of_12(self):
        from axiom.extensions.builtins.classroom.sim import classroom_of_12

        students = classroom_of_12()
        assert len(students) == 12
        # Mixed languages
        langs = {s.language for s in students}
        assert len(langs) >= 2
        # Mixed timezones
        offsets = {s.timezone_offset_hours for s in students}
        assert len(offsets) >= 4

    def test_engagement_rates(self):
        from axiom.extensions.builtins.classroom.sim import SimStudent

        high = SimStudent("x", "x", "physics", "en", "high", 0)
        low = SimStudent("y", "y", "general", "en", "low", 0)
        assert high.sessions_per_day_mean() > low.sessions_per_day_mean()


# ---------------------------------------------------------------------------
# Rubric
# ---------------------------------------------------------------------------


class TestRubric:
    def test_response_with_citation_scores_high_on_citation(self):
        from axiom.extensions.builtins.classroom.sim import score_response

        rs = score_response(
            query="what is fission?",
            response="Fission splits nuclei [source: ch3]. First, ...",
            student_id="s1",
            student_profile={"pedagogy_preference": "didactic"},
            intent_id="teaching",
        )
        assert rs.has_citation == 1.0
        assert rs.composite > 0.5

    def test_canned_refusal_zeroes_out(self):
        from axiom.extensions.builtins.classroom.sim import score_response

        rs = score_response(
            query="what is fission?",
            response="I cannot help with that.",
            student_id="s1",
            student_profile={},
            intent_id="teaching",
        )
        assert rs.no_refusal == 0.0
        assert rs.composite < 0.5

    def test_socratic_preference_rewards_questions(self):
        from axiom.extensions.builtins.classroom.sim import score_response

        socratic = score_response(
            query="what is fission?",
            response="What do you already know? Have you considered the mass difference?",
            student_id="s1",
            student_profile={"pedagogy_preference": "socratic"},
            intent_id="teaching",
        )
        didactic_same = score_response(
            query="what is fission?",
            response="What do you already know? Have you considered the mass difference?",
            student_id="s1",
            student_profile={"pedagogy_preference": "didactic"},
            intent_id="teaching",
        )
        assert socratic.profile_aligned > didactic_same.profile_aligned

    def test_llm_judge_populates_extra_fields(self):
        from axiom.extensions.builtins.classroom.sim import score_response

        def fake_judge(**kw):
            return {
                "factual_correctness": 0.9,
                "pedagogical_appropriateness": 0.8,
                "rationale": "LLM says accurate.",
            }

        rs = score_response(
            query="what is fission?",
            response="Fission splits atoms.",
            student_id="s1",
            student_profile={},
            intent_id="teaching",
            llm_judge=fake_judge,
        )
        assert rs.factual_correctness == 0.9
        assert rs.pedagogical_appropriateness == 0.8
        assert "LLM says accurate." in rs.rationale


# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------


class TestHarness:
    def test_run_simulation_produces_artifacts(self, tmp_path, monkeypatch):
        monkeypatch.setenv("AXIOM_RUNTIME_ROOT", str(tmp_path))
        from axiom.extensions.builtins.classroom.sim import (
            classroom_of_12,
            run_simulation,
        )

        students = classroom_of_12()[:3]  # smaller for test
        result = run_simulation(
            students=students,
            classroom_id="sim-test",
            turns_per_student=2,
            out_dir=tmp_path / "sim_run",
        )

        assert result.turns_simulated == 6
        # Every turn should pass the composition audit
        assert result.composition_pass_count == 6
        assert result.composition_fail_count == 0

        # Artifacts written
        assert (tmp_path / "sim_run" / "traces.jsonl").exists()
        assert (tmp_path / "sim_run" / "chalke_scores.jsonl").exists()
        assert (tmp_path / "sim_run" / "metrics.json").exists()
        assert (tmp_path / "sim_run" / "composition_audit.json").exists()

    def test_mean_score_computed(self, tmp_path, monkeypatch):
        monkeypatch.setenv("AXIOM_RUNTIME_ROOT", str(tmp_path))
        from axiom.extensions.builtins.classroom.sim import (
            classroom_of_12,
            run_simulation,
        )

        students = classroom_of_12()[:2]
        result = run_simulation(
            students=students,
            classroom_id="sim-score",
            turns_per_student=2,
            out_dir=tmp_path / "sim_run",
        )
        assert 0.0 <= result.mean_score <= 1.0
        # Canned responses should score reasonably (they include citations
        # + structure + question framing)
        assert result.mean_score > 0.4

    def test_metrics_json_shape(self, tmp_path, monkeypatch):
        monkeypatch.setenv("AXIOM_RUNTIME_ROOT", str(tmp_path))
        from axiom.extensions.builtins.classroom.sim import (
            classroom_of_12,
            run_simulation,
        )

        students = classroom_of_12()[:2]
        run_simulation(
            students=students,
            classroom_id="cr-metrics",
            turns_per_student=1,
            out_dir=tmp_path / "sim_run",
        )
        metrics = json.loads((tmp_path / "sim_run" / "metrics.json").read_text())
        assert metrics["classroom_id"] == "cr-metrics"
        assert metrics["turns_simulated"] == 2
        assert metrics["composition_pass_count"] == 2
        assert "per_student_scores" in metrics

    def test_composition_audit_includes_every_turn(self, tmp_path, monkeypatch):
        monkeypatch.setenv("AXIOM_RUNTIME_ROOT", str(tmp_path))
        from axiom.extensions.builtins.classroom.sim import (
            classroom_of_12,
            run_simulation,
        )

        students = classroom_of_12()[:2]
        run_simulation(
            students=students,
            classroom_id="cr-audit",
            turns_per_student=3,
            out_dir=tmp_path / "sim_run",
        )
        audit = json.loads(
            (tmp_path / "sim_run" / "composition_audit.json").read_text()
        )
        assert len(audit) == 6
        assert all(entry["composition_pass"] for entry in audit)
