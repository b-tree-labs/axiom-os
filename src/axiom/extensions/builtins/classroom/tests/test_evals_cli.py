# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tests for `axi classroom evals <classroom_id> --bank FILE`.

Covers CLI wiring on top of the engine: bank loading, retrieval
against the local classroom index, and the pass/fail report. The
engine itself is tested in `test_classroom_evals.py`.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from axiom.extensions.builtins.classroom.classroom_local_index import (
    ClassroomLocalIndex,
)
from axiom.extensions.builtins.classroom.cli import main


def _seed_index(classroom_dir: Path, files: list[dict]) -> None:
    idx = ClassroomLocalIndex(base_dir=classroom_dir)
    idx.open()
    try:
        for f in files:
            idx.ingest(
                file_id=f["file_id"],
                title=f["title"],
                content=f["content"],
                embed=None,
            )
    finally:
        idx.close()


def _write_bank(path: Path, rows: list[dict]) -> None:
    with path.open("w") as f:
        for row in rows:
            f.write(json.dumps(row) + "\n")


@pytest.fixture
def home_with_class(tmp_path, monkeypatch):
    home = tmp_path / "h"
    monkeypatch.setenv("HOME", str(home))
    class_dir = home / ".axi" / "classrooms" / "NE101"
    class_dir.mkdir(parents=True)
    _seed_index(class_dir, files=[
        {
            "file_id": "f1",
            "title": "Chapter 1 — Control rods",
            "content": (
                "Control rods absorb neutrons to slow fission reactions."
            ),
        },
        {
            "file_id": "f2",
            "title": "Chapter 2 — Cooling",
            "content": (
                "The primary coolant loop transfers heat to the "
                "secondary loop via steam generators."
            ),
        },
    ])
    return home


@pytest.fixture
def canned_llm(monkeypatch):
    """Canned Gateway that synthesizes keyword-rich answers from citations."""

    class _Resp:
        success = True

        def __init__(self, prompt: str):
            self.text = _fake_answer_for(prompt)

    class _Gw:
        def __init__(self, *a, **kw): pass
        def complete(self, *, prompt, system, task):
            return _Resp(prompt)

    monkeypatch.setattr("axiom.infra.gateway.Gateway", _Gw)
    return _Gw


def _fake_answer_for(prompt: str) -> str:
    """Heuristic: pull any citation bodies out of the prompt and return
    them verbatim — the keyword scorer only cares about substrings."""
    p = prompt.lower()
    if "control rod" in p:
        return (
            "A control rod absorbs neutrons to slow fission "
            "[Chapter 1 — Control rods]."
        )
    if "cooling" in p or "coolant" in p:
        return (
            "The primary coolant loop transfers heat to the secondary "
            "loop via steam generators [Chapter 2 — Cooling]."
        )
    return "Not in the class materials."


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


class TestEvalsHappyPath:
    def test_all_pass_when_questions_match_class_content(
        self, home_with_class, tmp_path, canned_llm, capsys,
    ):
        bank = tmp_path / "bank.jsonl"
        _write_bank(bank, [
            {"question": "What is a control rod?",
             "expected_keywords": ["absorb", "neutron"]},
            {"question": "What is the primary coolant loop?",
             "expected_keywords": ["heat", "steam"]},
        ])
        rc = main(["evals", "NE101", "--bank", str(bank)])
        assert rc == 0
        out = capsys.readouterr().out
        assert "2/2 passed" in out

    def test_mixed_pass_fail_nonzero_exit(
        self, home_with_class, tmp_path, canned_llm, capsys,
    ):
        bank = tmp_path / "bank.jsonl"
        _write_bank(bank, [
            {"question": "What is a control rod?",
             "expected_keywords": ["absorb"]},
            {"question": "What color is the sky?",
             "expected_keywords": ["blue"]},  # not in class materials
        ])
        rc = main(["evals", "NE101", "--bank", str(bank)])
        assert rc == 1  # any failure → nonzero exit
        out = capsys.readouterr().out
        assert "1/2 passed" in out

    def test_json_output_shape(
        self, home_with_class, tmp_path, canned_llm, capsys,
    ):
        bank = tmp_path / "bank.jsonl"
        _write_bank(bank, [
            {"question": "control rod?", "expected_keywords": ["absorb"]},
        ])
        rc = main(["evals", "NE101", "--bank", str(bank), "--json"])
        assert rc == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["classroom_id"] == "NE101"
        assert payload["total"] == 1
        assert payload["passed"] == 1
        assert payload["pass_rate"] == 1.0
        assert len(payload["results"]) == 1
        assert payload["results"][0]["passed"] is True


# ---------------------------------------------------------------------------
# Error paths
# ---------------------------------------------------------------------------


class TestBaselineFlag:
    def test_baseline_runs_and_reports_lift(
        self, home_with_class, tmp_path, canned_llm, capsys,
    ):
        bank = tmp_path / "bank.jsonl"
        _write_bank(bank, [
            # Class-specific — Axiom should win, baseline likely won't
            # (our canned_llm returns same answer regardless of system).
            {"question": "What is a control rod?",
             "expected_keywords": ["absorb", "neutron"]},
        ])
        main(["evals", "NE101", "--bank", str(bank), "--baseline"])
        # Might be 0 or 1 depending on whether baseline also passes;
        # just verify the output mentions both paths.
        out = capsys.readouterr().out
        assert "Baseline" in out
        assert "Lift" in out

    def test_baseline_json_output_includes_lift(
        self, home_with_class, tmp_path, canned_llm, capsys,
    ):
        bank = tmp_path / "bank.jsonl"
        _write_bank(bank, [
            {"question": "What is a control rod?",
             "expected_keywords": ["absorb"]},
        ])
        main(["evals", "NE101", "--bank", str(bank), "--baseline", "--json"])
        payload = json.loads(capsys.readouterr().out)
        assert "baseline" in payload
        assert "lift" in payload
        assert "axiom_only_wins" in payload
        assert "baseline_only_wins" in payload

    def test_baseline_skipped_when_cite_only(
        self, home_with_class, tmp_path, canned_llm, capsys,
    ):
        """cite-only short-circuits both the Axiom and baseline LLM
        calls, so --baseline is silently ignored rather than surprising
        the instructor with an LLM invocation."""
        bank = tmp_path / "bank.jsonl"
        _write_bank(bank, [
            {"question": "q", "expected_keywords": ["x"]},
        ])
        main([
            "evals", "NE101", "--bank", str(bank),
            "--baseline", "--cite-only", "--json",
        ])
        payload = json.loads(capsys.readouterr().out)
        assert "baseline" not in payload


class TestBankPresets:
    def test_bank_preset_resolves_to_shipped_bank(
        self, home_with_class, canned_llm, capsys,
    ):
        """`--bank-preset ne101-core` should find the shipped JSONL
        without the user knowing its path."""
        main([
            "evals", "NE101", "--bank-preset", "ne101-core", "--json",
        ])
        # Might be 0 or 1 depending on how many questions our canned
        # classroom happens to answer; just check the bank loaded.
        payload = json.loads(capsys.readouterr().out)
        assert payload["total"] >= 10

    def test_bank_preset_accepts_underscores_too(
        self, home_with_class, canned_llm, capsys,
    ):
        main([
            "evals", "NE101", "--bank-preset", "ne101_core", "--json",
        ])
        payload = json.loads(capsys.readouterr().out)
        assert payload["total"] >= 10

    def test_bank_preset_list(self, home_with_class, capsys):
        rc = main(["evals", "NE101", "--bank-preset", "list"])
        assert rc == 0
        out = capsys.readouterr().out
        assert "ne101_core" in out or "ne101-core" in out

    def test_bank_preset_unknown_name(self, home_with_class, capsys):
        rc = main([
            "evals", "NE101", "--bank-preset", "does-not-exist",
        ])
        assert rc == 1
        err = capsys.readouterr().err
        assert "no bank preset" in err.lower()

    def test_bank_and_bank_preset_are_mutually_exclusive(
        self, home_with_class, tmp_path, capsys,
    ):
        bank = tmp_path / "b.jsonl"
        _write_bank(bank, [{"question": "q", "expected_keywords": ["k"]}])
        import pytest as _pytest
        with _pytest.raises(SystemExit):
            main([
                "evals", "NE101",
                "--bank", str(bank),
                "--bank-preset", "ne101-core",
            ])


class TestInstructorSelfEval:
    """Instructor can run evals on their own coordinator materials
    without first joining their own class as a student."""

    def test_instructor_self_eval_from_coordinator_materials(
        self, tmp_path, monkeypatch, canned_llm, capsys,
    ):
        # Point HOME at a fresh tmp dir — no student-side index exists.
        home = tmp_path / "instructor-home"
        monkeypatch.setenv("HOME", str(home))
        # But the instructor DOES have coordinator materials.
        from axiom.extensions.builtins.classroom.classroom_materials import (
            ClassroomMaterialsStore,
        )
        materials = ClassroomMaterialsStore(
            home / ".axi" / "coordinator" / "classrooms" / "NE101"
        )
        materials.add_text(
            "Control rods absorb neutrons to slow fission reactions.",
            filename="ch1.md", title="Chapter 1 — Control rods",
        )

        bank = tmp_path / "bank.jsonl"
        _write_bank(bank, [
            {"question": "What is a control rod?",
             "expected_keywords": ["absorb", "neutron"]},
        ])

        rc = main(["evals", "NE101", "--bank", str(bank)])
        assert rc == 0
        out = capsys.readouterr().out
        assert "1/1 passed" in out

    def test_instructor_self_eval_narrates_transient_index(
        self, tmp_path, monkeypatch, canned_llm, capsys,
    ):
        home = tmp_path / "instructor-home"
        monkeypatch.setenv("HOME", str(home))
        from axiom.extensions.builtins.classroom.classroom_materials import (
            ClassroomMaterialsStore,
        )
        materials = ClassroomMaterialsStore(
            home / ".axi" / "coordinator" / "classrooms" / "NE101"
        )
        materials.add_text("content", filename="ch1.md", title="Ch 1")

        bank = tmp_path / "bank.jsonl"
        _write_bank(bank, [
            {"question": "q", "expected_keywords": ["k"]},
        ])
        main(["evals", "NE101", "--bank", str(bank)])
        captured = capsys.readouterr()
        combined = captured.out + captured.err
        # A friendly note so the instructor knows they're in self-eval
        # mode, not student mode.
        assert "coordinator" in combined.lower() or "your materials" in combined.lower()

    def test_neither_coord_nor_student_materials_errors(
        self, tmp_path, monkeypatch, capsys,
    ):
        home = tmp_path / "empty-home"
        monkeypatch.setenv("HOME", str(home))
        bank = tmp_path / "bank.jsonl"
        _write_bank(bank, [
            {"question": "q", "expected_keywords": ["k"]},
        ])
        rc = main(["evals", "NOT_A_REAL_CLASS", "--bank", str(bank)])
        assert rc == 1
        err = capsys.readouterr().err
        # Point the user at one of the two paths to fix it.
        assert ("join" in err.lower()) or ("prep corpus" in err.lower())


class TestMinPassRateGate:
    """The CI gate — `--min-pass-rate` relaxes the strict all-pass
    default into a threshold so corpus-under-construction states
    don't fail CI, but regressions still do."""

    def test_gate_passes_when_above_threshold(
        self, home_with_class, tmp_path, canned_llm, capsys,
    ):
        bank = tmp_path / "bank.jsonl"
        _write_bank(bank, [
            {"question": "What is a control rod?", "expected_keywords": ["absorb"]},
            {"question": "Unrelated",              "expected_keywords": ["zzz"]},
        ])
        # 1/2 pass = 50%. Threshold 0.5 → gate passes.
        rc = main([
            "evals", "NE101", "--bank", str(bank),
            "--min-pass-rate", "0.5",
        ])
        assert rc == 0

    def test_gate_fails_when_below_threshold(
        self, home_with_class, tmp_path, canned_llm, capsys,
    ):
        bank = tmp_path / "bank.jsonl"
        _write_bank(bank, [
            {"question": "What is a control rod?", "expected_keywords": ["absorb"]},
            {"question": "Unrelated",              "expected_keywords": ["zzz"]},
        ])
        # 1/2 pass = 50%. Threshold 0.75 → gate fails.
        rc = main([
            "evals", "NE101", "--bank", str(bank),
            "--min-pass-rate", "0.75",
        ])
        assert rc == 1
        captured = capsys.readouterr()
        combined = captured.out + captured.err
        assert "CI gate FAILED" in combined or "threshold" in combined.lower()

    def test_gate_passes_when_all_pass_and_threshold_met(
        self, home_with_class, tmp_path, canned_llm, capsys,
    ):
        bank = tmp_path / "bank.jsonl"
        _write_bank(bank, [
            {"question": "What is a control rod?",
             "expected_keywords": ["absorb"]},
        ])
        rc = main([
            "evals", "NE101", "--bank", str(bank),
            "--min-pass-rate", "1.0",
        ])
        assert rc == 0
        out = capsys.readouterr().out
        assert "CI gate" in out

    def test_gate_json_includes_threshold_fields(
        self, home_with_class, tmp_path, canned_llm, capsys,
    ):
        bank = tmp_path / "bank.jsonl"
        _write_bank(bank, [
            {"question": "q1", "expected_keywords": ["control"]},
            {"question": "q2", "expected_keywords": ["control"]},
        ])
        main([
            "evals", "NE101", "--bank", str(bank),
            "--min-pass-rate", "0.5", "--json",
        ])
        data = json.loads(capsys.readouterr().out)
        assert data["min_pass_rate"] == 0.5
        assert "gate_passed" in data

    def test_without_gate_strict_default_preserved(
        self, home_with_class, tmp_path, canned_llm, capsys,
    ):
        """Regression: omitting --min-pass-rate keeps the existing
        "any failure → exit 1" behavior."""
        bank = tmp_path / "bank.jsonl"
        _write_bank(bank, [
            {"question": "q", "expected_keywords": ["absurd-keyword"]},
        ])
        rc = main(["evals", "NE101", "--bank", str(bank)])
        assert rc == 1


class TestEvalsErrors:
    def test_missing_bank_file(self, home_with_class, tmp_path, capsys):
        rc = main(["evals", "NE101", "--bank", str(tmp_path / "nope.jsonl")])
        assert rc == 1
        err = capsys.readouterr().err
        assert "not found" in err.lower() or "bank" in err.lower()

    def test_malformed_bank(self, home_with_class, tmp_path, capsys):
        bad = tmp_path / "bank.jsonl"
        bad.write_text('{"question": "q"}\n')  # missing expected_keywords
        rc = main(["evals", "NE101", "--bank", str(bad)])
        assert rc == 1
        err = capsys.readouterr().err
        assert "expected_keywords" in err.lower() or "invalid" in err.lower()

    def test_not_a_member_classroom(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setenv("HOME", str(tmp_path / "empty-home"))
        bank = tmp_path / "bank.jsonl"
        _write_bank(bank, [{"question": "q", "expected_keywords": ["k"]}])
        rc = main(["evals", "NEVER_JOINED", "--bank", str(bank)])
        assert rc == 1
        err = capsys.readouterr().err
        # Points student at what they need.
        assert "join" in err.lower()
