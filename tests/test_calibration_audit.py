# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tests for ``scripts/calibration_audit.py``.

Everything here runs offline against the mock adapter and synthetic data —
no network, no external decision API, ever. The external seam's test proves
exactly that: it refuses to act.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

# Load the script as a module (scripts/ isn't a package).
_SCRIPT = Path(__file__).parent.parent / "scripts" / "calibration_audit.py"
_spec = importlib.util.spec_from_file_location("calibration_audit", _SCRIPT)
assert _spec and _spec.loader
ca = importlib.util.module_from_spec(_spec)
sys.modules["calibration_audit"] = ca
_spec.loader.exec_module(ca)


# ---------------------------------------------------------------------------
# Vendored calibration math
# ---------------------------------------------------------------------------


class TestReliabilityCurve:
    def test_bins_partition_all_samples(self):
        confs = [0.05, 0.15, 0.95, 0.95, 1.0]
        correct = [True, False, True, True, True]
        curve = ca.reliability_curve(confs, correct, n_bins=10)
        assert sum(b.count for b in curve) == 5
        assert curve[0].count == 1  # 0.05
        assert curve[1].count == 1  # 0.15
        assert curve[9].count == 3  # 0.95, 0.95, and 1.0 folds into last bin

    def test_bin_accuracy_and_confidence(self):
        confs = [0.95, 0.95]
        correct = [True, False]
        curve = ca.reliability_curve(confs, correct, n_bins=10)
        top = curve[9]
        assert top.avg_confidence == pytest.approx(0.95)
        assert top.accuracy == pytest.approx(0.5)

    def test_length_mismatch_raises(self):
        with pytest.raises(ValueError):
            ca.reliability_curve([0.5], [True, False])


class TestECE:
    def test_hand_computed_two_bins(self):
        # Bin A: 2 samples, conf 0.95, acc 0.5 -> gap 0.45
        # Bin B: 2 samples, conf 0.55, acc 0.5 -> gap 0.05
        # ECE = 0.5*0.45 + 0.5*0.05 = 0.25
        confs = [0.95, 0.95, 0.55, 0.55]
        correct = [True, False, True, False]
        curve = ca.reliability_curve(confs, correct, n_bins=10)
        assert ca.expected_calibration_error(curve) == pytest.approx(0.25)
        assert ca.max_calibration_error(curve) == pytest.approx(0.45)

    def test_perfectly_calibrated_is_zero(self):
        # conf 0.75 bucket where exactly 3 of 4 are correct.
        confs = [0.75] * 4
        correct = [True, True, True, False]
        curve = ca.reliability_curve(confs, correct, n_bins=10)
        assert ca.expected_calibration_error(curve) == pytest.approx(0.0)

    def test_zero_samples_raises(self):
        curve = ca.reliability_curve([], [], n_bins=10)
        with pytest.raises(ValueError):
            ca.expected_calibration_error(curve)


class TestBrier:
    def test_hand_computed(self):
        # (0.8-1)^2 = 0.04 ; (0.8-0)^2 = 0.64 -> mean 0.34
        assert ca.brier_score([0.8, 0.8], [True, False]) == pytest.approx(0.34)


# ---------------------------------------------------------------------------
# Contract validation
# ---------------------------------------------------------------------------


def _q(options=("a", "b")):
    return ca.Question(id="q0", text="pick", options=tuple(options))


class TestValidateAnswer:
    def test_valid_answer_passes(self):
        ans = ca.Answer(id="q0", choice="a", distribution={"a": 0.7, "b": 0.3})
        assert ca.validate_answer(ans, _q()).confidence == pytest.approx(0.7)

    def test_bad_sum_is_protocol_error(self):
        ans = ca.Answer(id="q0", choice="a", distribution={"a": 0.7, "b": 0.7})
        with pytest.raises(ca.ProtocolError, match="sums to"):
            ca.validate_answer(ans, _q())

    def test_choice_outside_schema_is_protocol_error(self):
        ans = ca.Answer(id="q0", choice="z", distribution={"a": 1.0})
        with pytest.raises(ca.ProtocolError, match="not in schema"):
            ca.validate_answer(ans, _q())

    def test_option_outside_schema_is_protocol_error(self):
        ans = ca.Answer(id="q0", choice="a",
                        distribution={"a": 0.5, "b": 0.3, "z": 0.2})
        with pytest.raises(ca.ProtocolError, match="outside the schema"):
            ca.validate_answer(ans, _q())


# ---------------------------------------------------------------------------
# Mock adapter
# ---------------------------------------------------------------------------


class TestMockAdapter:
    def test_deterministic_across_instances(self):
        q = _q(("x", "y", "z"))
        a1 = ca.MockAdapter(seed=7).decide("state text", [q], gold={"q0": "x"})
        a2 = ca.MockAdapter(seed=7).decide("state text", [q], gold={"q0": "x"})
        assert a1[0].choice == a2[0].choice
        assert a1[0].distribution == a2[0].distribution

    def test_distribution_is_contract_valid(self):
        q = _q(("x", "y", "z"))
        (ans,) = ca.MockAdapter().decide("s", [q], gold={"q0": "y"})
        assert sum(ans.distribution.values()) == pytest.approx(1.0)
        assert ans.choice in q.options

    def test_accuracy_knob_moves_empirical_accuracy(self):
        options = ("a", "b", "c", "d")
        examples = [ca.Example(text=f"doc {i}", label=options[i % 4])
                    for i in range(400)]
        strong = ca.run_audit(ca.MockAdapter(accuracy=0.9, stated_confidence=0.9),
                              examples, options, min_samples=10)
        weak = ca.run_audit(ca.MockAdapter(accuracy=0.3, stated_confidence=0.9),
                            examples, options, min_samples=10)
        assert strong.verdict.accuracy > 0.8
        assert weak.verdict.accuracy < 0.5


# ---------------------------------------------------------------------------
# End-to-end audit with the mock adapter
# ---------------------------------------------------------------------------


class TestRunAudit:
    def _examples(self, n=300, options=("a", "b", "c")):
        return ([ca.Example(text=f"doc {i}", label=options[i % len(options)])
                 for i in range(n)], options)

    def test_calibrated_endpoint_passes_gate(self):
        examples, options = self._examples()
        adapter = ca.MockAdapter(accuracy=0.8, stated_confidence=0.8)
        report = ca.run_audit(adapter, examples, options,
                              ece_threshold=0.08, min_samples=100)
        assert report.verdict.passed, report.verdict.reasons
        assert report.verdict.n == 300

    def test_overconfident_endpoint_fails_gate_with_reason(self):
        examples, options = self._examples()
        adapter = ca.MockAdapter(accuracy=0.5, stated_confidence=0.95)
        report = ca.run_audit(adapter, examples, options,
                              ece_threshold=0.05, min_samples=100)
        assert not report.verdict.passed
        assert any("ECE" in r for r in report.verdict.reasons)
        # The gap is ~0.45 and the metric should see most of it.
        assert report.verdict.ece > 0.3

    def test_insufficient_volume_fails_gate(self):
        examples, options = self._examples(n=20)
        report = ca.run_audit(ca.MockAdapter(), examples, options,
                              min_samples=100)
        assert not report.verdict.passed
        assert any("insufficient volume" in r for r in report.verdict.reasons)

    def test_report_serializes_with_curve(self):
        examples, options = self._examples(n=150)
        report = ca.run_audit(ca.MockAdapter(), examples, options)
        d = report.to_dict()
        json.dumps(d)  # round-trippable
        assert len(d["reliability_curve"]) == 10
        assert sum(b["count"] for b in d["reliability_curve"]) == 150
        assert d["adapter"] == "mock"


# ---------------------------------------------------------------------------
# The external seam must refuse to act
# ---------------------------------------------------------------------------


class TestExternalStub:
    def test_blocked_without_approval_or_credential(self, monkeypatch):
        monkeypatch.delenv("CALIBRATION_AUDIT_EXTERNAL_APPROVED", raising=False)
        monkeypatch.delenv("CALIBRATION_AUDIT_API_KEY", raising=False)
        stub = ca.ExternalEndpointStub(endpoint="https://example.invalid/v1/decide")
        with pytest.raises(ca.ExternalCallBlocked, match="credential AND explicit"):
            stub.decide("state", [_q()])

    def test_approval_alone_is_not_enough(self, monkeypatch):
        monkeypatch.setenv("CALIBRATION_AUDIT_EXTERNAL_APPROVED", "1")
        monkeypatch.delenv("CALIBRATION_AUDIT_API_KEY", raising=False)
        stub = ca.ExternalEndpointStub(endpoint="https://example.invalid/v1/decide")
        with pytest.raises(ca.ExternalCallBlocked):
            stub.decide("state", [_q()])

    def test_even_armed_it_has_no_transport(self, monkeypatch):
        # With both gates satisfied there is still no provider adapter —
        # the stub raises rather than performing any I/O.
        monkeypatch.setenv("CALIBRATION_AUDIT_EXTERNAL_APPROVED", "1")
        monkeypatch.setenv("CALIBRATION_AUDIT_API_KEY", "test-key-never-used")
        stub = ca.ExternalEndpointStub(endpoint="https://example.invalid/v1/decide")
        with pytest.raises(NotImplementedError, match="No network I/O"):
            stub.decide("state", [_q()])


# ---------------------------------------------------------------------------
# Dataset loading (offline forms only)
# ---------------------------------------------------------------------------


class TestLoadExamples:
    def test_synthetic(self):
        examples, options = ca.load_examples("synthetic", limit=10, seed=1)
        assert len(examples) == 10
        assert options == ("alpha", "beta")

    def test_jsonl(self, tmp_path):
        p = tmp_path / "data.jsonl"
        rows = [{"text": "hello", "label": "x"}, {"text": "world", "label": "y"}]
        p.write_text("\n".join(json.dumps(r) for r in rows), encoding="utf-8")
        examples, options = ca.load_examples(f"jsonl:{p}", limit=10, seed=1)
        assert [e.label for e in examples] == ["x", "y"]
        assert options == ("x", "y")

    def test_unknown_spec_exits(self):
        with pytest.raises(SystemExit):
            ca.load_examples("nope", limit=1, seed=1)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


class TestCli:
    def test_mock_synthetic_end_to_end(self, tmp_path, capsys):
        out = tmp_path / "report.json"
        rc = ca.main([
            "--adapter", "mock", "--dataset", "synthetic",
            "--limit", "200", "--ece-threshold", "0.08",
            "--out", str(out),
        ])
        assert rc == 0
        report = json.loads(out.read_text(encoding="utf-8"))
        assert report["gate"]["passed"] is True
        assert report["n"] == 200

    def test_external_requires_endpoint(self):
        with pytest.raises(SystemExit):
            ca.main(["--adapter", "external"])
