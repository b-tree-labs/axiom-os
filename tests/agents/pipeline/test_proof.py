# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""TDD tests for ProofSpec types + verifier harness (ADR-034 §D2/§D9, task #71)."""

from __future__ import annotations

import hashlib
import subprocess

import pytest

# ---------------------------------------------------------------------------
# Core dataclasses + enum
# ---------------------------------------------------------------------------


class TestProofTypes:
    def test_proof_type_enum_values(self):
        from axiom.agents.pipeline.proof import ProofType

        assert ProofType.TEST.value == "test"
        assert ProofType.TYPECHECK.value == "typecheck"
        assert ProofType.STRUCTURAL.value == "structural"
        assert ProofType.RETRIEVAL.value == "retrieval"
        assert ProofType.ATTESTATION.value == "attestation"
        assert ProofType.REPLAY.value == "replay"
        assert ProofType.NULL.value == "null"

    def test_proof_spec_is_frozen(self):
        from dataclasses import FrozenInstanceError

        from axiom.agents.pipeline.proof import ProofSpec, ProofType

        spec = ProofSpec(proof_type=ProofType.NULL, parameters={"reason": "draft"})
        with pytest.raises(FrozenInstanceError):
            spec.description = "mutated"  # type: ignore[misc]

    def test_proof_spec_default_parameters(self):
        from axiom.agents.pipeline.proof import ProofSpec, ProofType

        spec = ProofSpec(proof_type=ProofType.TEST)
        assert spec.parameters == {}
        assert spec.description == ""


# ---------------------------------------------------------------------------
# TestProofVerifier — shell-out style
# ---------------------------------------------------------------------------


class _StubProcess:
    """Minimal CompletedProcess substitute for the runner stubs."""

    def __init__(self, returncode: int, stdout: str = "", stderr: str = ""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class TestTestProofVerifier:
    def test_success_when_all_commands_pass(self):
        from axiom.agents.pipeline.proof import (
            ProofSpec,
            ProofType,
            TestProofVerifier,
        )

        runner_calls: list[str] = []

        def runner(cmd: str) -> _StubProcess:
            runner_calls.append(cmd)
            return _StubProcess(returncode=0, stdout="ok", stderr="")

        verifier = TestProofVerifier(runner=runner)
        spec = ProofSpec(
            proof_type=ProofType.TEST,
            parameters={"commands": ["pytest tests/foo.py", "pytest tests/bar.py"]},
        )

        result = verifier.verify(spec, step_outputs={})

        assert result.success is True
        assert runner_calls == ["pytest tests/foo.py", "pytest tests/bar.py"]
        assert result.artifact is not None
        assert result.artifact.success is True
        assert "exit_codes" in result.artifact.captured_outputs

    def test_failure_when_any_command_fails(self):
        from axiom.agents.pipeline.proof import (
            ProofSpec,
            ProofType,
            TestProofVerifier,
        )

        def runner(cmd: str) -> _StubProcess:
            if "bar" in cmd:
                return _StubProcess(returncode=1, stdout="", stderr="boom")
            return _StubProcess(returncode=0)

        verifier = TestProofVerifier(runner=runner)
        spec = ProofSpec(
            proof_type=ProofType.TEST,
            parameters={"commands": ["pytest foo", "pytest bar"]},
        )

        result = verifier.verify(spec, step_outputs={})

        assert result.success is False
        assert result.artifact is not None
        assert result.artifact.success is False
        assert "boom" in result.artifact.captured_outputs.get("stderr", [""])[1]

    def test_missing_commands_fails(self):
        from axiom.agents.pipeline.proof import (
            ProofSpec,
            ProofType,
            TestProofVerifier,
        )

        verifier = TestProofVerifier(runner=lambda c: _StubProcess(0))
        spec = ProofSpec(proof_type=ProofType.TEST, parameters={})
        result = verifier.verify(spec, step_outputs={})
        assert result.success is False
        assert "commands" in result.rationale

    def test_default_runner_is_subprocess_run(self):
        # Don't actually shell out; just verify the default attribute exists
        # and is a callable. We don't want module-import or constructor-time IO.
        from axiom.agents.pipeline.proof import TestProofVerifier

        verifier = TestProofVerifier()
        assert callable(verifier.runner)


# ---------------------------------------------------------------------------
# TypecheckProofVerifier
# ---------------------------------------------------------------------------


class TestTypecheckProofVerifier:
    def test_success_when_runner_clean(self):
        from axiom.agents.pipeline.proof import (
            ProofSpec,
            ProofType,
            TypecheckProofVerifier,
        )

        captured: list[str] = []

        def runner(cmd: str) -> _StubProcess:
            captured.append(cmd)
            return _StubProcess(returncode=0, stdout="Success", stderr="")

        verifier = TypecheckProofVerifier(runner=runner)
        spec = ProofSpec(
            proof_type=ProofType.TYPECHECK,
            parameters={"paths": ["src/axiom/agents"]},
        )

        result = verifier.verify(spec, step_outputs={})
        assert result.success is True
        assert captured  # runner was called

    def test_failure_when_runner_reports_errors(self):
        from axiom.agents.pipeline.proof import (
            ProofSpec,
            ProofType,
            TypecheckProofVerifier,
        )

        def runner(cmd: str) -> _StubProcess:
            return _StubProcess(returncode=1, stdout="", stderr="error")

        verifier = TypecheckProofVerifier(runner=runner)
        spec = ProofSpec(
            proof_type=ProofType.TYPECHECK,
            parameters={"paths": ["src/axiom"]},
        )

        result = verifier.verify(spec, step_outputs={})
        assert result.success is False
        assert result.artifact is not None
        assert result.artifact.success is False

    def test_missing_paths_fails(self):
        from axiom.agents.pipeline.proof import (
            ProofSpec,
            ProofType,
            TypecheckProofVerifier,
        )

        verifier = TypecheckProofVerifier(runner=lambda c: _StubProcess(0))
        spec = ProofSpec(proof_type=ProofType.TYPECHECK, parameters={})
        result = verifier.verify(spec, step_outputs={})
        assert result.success is False


# ---------------------------------------------------------------------------
# StructuralProofVerifier
# ---------------------------------------------------------------------------


class TestStructuralProofVerifier:
    def test_file_exists_success(self, tmp_path):
        from axiom.agents.pipeline.proof import (
            ProofSpec,
            ProofType,
            StructuralAssertion,
            StructuralProofVerifier,
        )

        target = tmp_path / "hello.txt"
        target.write_text("hi")

        spec = ProofSpec(
            proof_type=ProofType.STRUCTURAL,
            parameters={
                "assertions": [
                    StructuralAssertion(kind="file_exists", path=str(target), expected=""),
                ]
            },
        )

        verifier = StructuralProofVerifier()
        result = verifier.verify(spec, step_outputs={})
        assert result.success is True

    def test_file_exists_failure_when_absent(self, tmp_path):
        from axiom.agents.pipeline.proof import (
            ProofSpec,
            ProofType,
            StructuralAssertion,
            StructuralProofVerifier,
        )

        missing = tmp_path / "nope.txt"
        spec = ProofSpec(
            proof_type=ProofType.STRUCTURAL,
            parameters={
                "assertions": [
                    StructuralAssertion(kind="file_exists", path=str(missing), expected=""),
                ]
            },
        )

        verifier = StructuralProofVerifier()
        result = verifier.verify(spec, step_outputs={})
        assert result.success is False
        assert result.artifact is not None
        assert "nope.txt" in result.rationale or "nope.txt" in str(
            result.artifact.captured_outputs
        )

    def test_function_signature_success(self, tmp_path):
        from axiom.agents.pipeline.proof import (
            ProofSpec,
            ProofType,
            StructuralAssertion,
            StructuralProofVerifier,
        )

        src = tmp_path / "mod.py"
        src.write_text(
            "def greet(name: str, formal: bool = False) -> str:\n    return name\n"
        )

        spec = ProofSpec(
            proof_type=ProofType.STRUCTURAL,
            parameters={
                "assertions": [
                    StructuralAssertion(
                        kind="function_signature",
                        path=str(src),
                        expected="greet(name, formal)",
                    ),
                ]
            },
        )

        verifier = StructuralProofVerifier()
        result = verifier.verify(spec, step_outputs={})
        assert result.success is True

    def test_function_signature_failure_when_signature_differs(self, tmp_path):
        from axiom.agents.pipeline.proof import (
            ProofSpec,
            ProofType,
            StructuralAssertion,
            StructuralProofVerifier,
        )

        src = tmp_path / "mod.py"
        src.write_text("def greet(name: str) -> str:\n    return name\n")

        spec = ProofSpec(
            proof_type=ProofType.STRUCTURAL,
            parameters={
                "assertions": [
                    StructuralAssertion(
                        kind="function_signature",
                        path=str(src),
                        expected="greet(name, formal)",
                    ),
                ]
            },
        )

        verifier = StructuralProofVerifier()
        result = verifier.verify(spec, step_outputs={})
        assert result.success is False

    def test_class_attribute_success(self, tmp_path):
        from axiom.agents.pipeline.proof import (
            ProofSpec,
            ProofType,
            StructuralAssertion,
            StructuralProofVerifier,
        )

        src = tmp_path / "mod.py"
        src.write_text(
            "class Foo:\n    bar: int = 1\n    def method(self):\n        pass\n"
        )

        spec = ProofSpec(
            proof_type=ProofType.STRUCTURAL,
            parameters={
                "assertions": [
                    StructuralAssertion(
                        kind="class_attribute",
                        path=str(src),
                        expected="Foo.bar",
                    ),
                ]
            },
        )

        verifier = StructuralProofVerifier()
        result = verifier.verify(spec, step_outputs={})
        assert result.success is True

    def test_class_attribute_failure(self, tmp_path):
        from axiom.agents.pipeline.proof import (
            ProofSpec,
            ProofType,
            StructuralAssertion,
            StructuralProofVerifier,
        )

        src = tmp_path / "mod.py"
        src.write_text("class Foo:\n    pass\n")

        spec = ProofSpec(
            proof_type=ProofType.STRUCTURAL,
            parameters={
                "assertions": [
                    StructuralAssertion(
                        kind="class_attribute",
                        path=str(src),
                        expected="Foo.bar",
                    ),
                ]
            },
        )

        verifier = StructuralProofVerifier()
        result = verifier.verify(spec, step_outputs={})
        assert result.success is False


# ---------------------------------------------------------------------------
# RetrievalProofVerifier
# ---------------------------------------------------------------------------


class TestRetrievalProofVerifier:
    def test_success_when_thresholds_met(self):
        from axiom.agents.pipeline.proof import (
            ProofSpec,
            ProofType,
            RetrievalProofVerifier,
        )

        spec = ProofSpec(
            proof_type=ProofType.RETRIEVAL,
            parameters={
                "min_citations": 2,
                "min_score": 0.5,
                "recency_window_days": 365,
            },
        )

        step_outputs = {
            "citations": [
                {"score": 0.9, "recency_days": 10},
                {"score": 0.7, "recency_days": 100},
                {"score": 0.6, "recency_days": 200},
            ]
        }

        verifier = RetrievalProofVerifier()
        result = verifier.verify(spec, step_outputs=step_outputs)
        assert result.success is True

    def test_failure_when_too_few_citations(self):
        from axiom.agents.pipeline.proof import (
            ProofSpec,
            ProofType,
            RetrievalProofVerifier,
        )

        spec = ProofSpec(
            proof_type=ProofType.RETRIEVAL,
            parameters={
                "min_citations": 3,
                "min_score": 0.5,
                "recency_window_days": 365,
            },
        )

        step_outputs = {"citations": [{"score": 0.9, "recency_days": 10}]}
        verifier = RetrievalProofVerifier()
        result = verifier.verify(spec, step_outputs=step_outputs)
        assert result.success is False

    def test_failure_when_score_below_threshold(self):
        from axiom.agents.pipeline.proof import (
            ProofSpec,
            ProofType,
            RetrievalProofVerifier,
        )

        spec = ProofSpec(
            proof_type=ProofType.RETRIEVAL,
            parameters={
                "min_citations": 1,
                "min_score": 0.8,
                "recency_window_days": 365,
            },
        )

        step_outputs = {"citations": [{"score": 0.3, "recency_days": 10}]}
        verifier = RetrievalProofVerifier()
        result = verifier.verify(spec, step_outputs=step_outputs)
        assert result.success is False

    def test_failure_when_citation_too_old(self):
        from axiom.agents.pipeline.proof import (
            ProofSpec,
            ProofType,
            RetrievalProofVerifier,
        )

        spec = ProofSpec(
            proof_type=ProofType.RETRIEVAL,
            parameters={
                "min_citations": 1,
                "min_score": 0.5,
                "recency_window_days": 30,
            },
        )

        step_outputs = {"citations": [{"score": 0.9, "recency_days": 100}]}
        verifier = RetrievalProofVerifier()
        result = verifier.verify(spec, step_outputs=step_outputs)
        assert result.success is False

    def test_missing_citations_fails(self):
        from axiom.agents.pipeline.proof import (
            ProofSpec,
            ProofType,
            RetrievalProofVerifier,
        )

        spec = ProofSpec(
            proof_type=ProofType.RETRIEVAL,
            parameters={"min_citations": 1, "min_score": 0.5, "recency_window_days": 30},
        )
        verifier = RetrievalProofVerifier()
        result = verifier.verify(spec, step_outputs={})
        assert result.success is False


# ---------------------------------------------------------------------------
# AttestationProofVerifier
# ---------------------------------------------------------------------------


class TestAttestationProofVerifier:
    def test_success_with_required_attesters(self):
        from axiom.agents.pipeline.proof import (
            AttestationProofVerifier,
            ProofSpec,
            ProofType,
        )

        verified_calls: list[tuple[str, str]] = []

        def signature_verifier(principal_id: str, signature: str) -> bool:
            verified_calls.append((principal_id, signature))
            return True

        spec = ProofSpec(
            proof_type=ProofType.ATTESTATION,
            parameters={
                "required_attesters": ["@alice:example-org", "@bob:example-org"],
                "min_attesters": 2,
            },
        )

        step_outputs = {
            "attestations": [
                {"principal_id": "@alice:example-org", "signature": "sig-a", "attested_at": "2026-04-27"},
                {"principal_id": "@bob:example-org", "signature": "sig-b", "attested_at": "2026-04-27"},
            ]
        }

        verifier = AttestationProofVerifier(signature_verifier=signature_verifier)
        result = verifier.verify(spec, step_outputs=step_outputs)
        assert result.success is True
        assert len(verified_calls) == 2

    def test_failure_when_signature_invalid(self):
        from axiom.agents.pipeline.proof import (
            AttestationProofVerifier,
            ProofSpec,
            ProofType,
        )

        def signature_verifier(principal_id: str, signature: str) -> bool:
            return signature != "bogus"

        spec = ProofSpec(
            proof_type=ProofType.ATTESTATION,
            parameters={"required_attesters": ["@alice:example-org"]},
        )
        step_outputs = {
            "attestations": [
                {"principal_id": "@alice:example-org", "signature": "bogus", "attested_at": "x"}
            ]
        }

        verifier = AttestationProofVerifier(signature_verifier=signature_verifier)
        result = verifier.verify(spec, step_outputs=step_outputs)
        assert result.success is False

    def test_failure_when_required_attester_missing(self):
        from axiom.agents.pipeline.proof import (
            AttestationProofVerifier,
            ProofSpec,
            ProofType,
        )

        spec = ProofSpec(
            proof_type=ProofType.ATTESTATION,
            parameters={
                "required_attesters": ["@alice:example-org", "@bob:example-org"],
                "min_attesters": 1,
            },
        )
        step_outputs = {
            "attestations": [
                {"principal_id": "@alice:example-org", "signature": "ok", "attested_at": "x"}
            ]
        }

        verifier = AttestationProofVerifier(
            signature_verifier=lambda p, s: True
        )
        result = verifier.verify(spec, step_outputs=step_outputs)
        assert result.success is False

    def test_failure_when_count_below_min(self):
        from axiom.agents.pipeline.proof import (
            AttestationProofVerifier,
            ProofSpec,
            ProofType,
        )

        spec = ProofSpec(
            proof_type=ProofType.ATTESTATION,
            parameters={"required_attesters": [], "min_attesters": 2},
        )
        step_outputs = {"attestations": []}
        verifier = AttestationProofVerifier(signature_verifier=lambda p, s: True)
        result = verifier.verify(spec, step_outputs=step_outputs)
        assert result.success is False


# ---------------------------------------------------------------------------
# ReplayProofVerifier
# ---------------------------------------------------------------------------


class TestReplayProofVerifier:
    def test_success_when_fingerprint_matches(self):
        from axiom.agents.pipeline.proof import (
            ProofSpec,
            ProofType,
            ReplayProofVerifier,
        )

        payload = b"deterministic output"
        fingerprint = hashlib.sha256(payload).hexdigest()

        spec = ProofSpec(
            proof_type=ProofType.REPLAY,
            parameters={"expected_fingerprint": fingerprint},
        )

        verifier = ReplayProofVerifier()
        result = verifier.verify(spec, step_outputs={"output_canonical": payload})
        assert result.success is True

    def test_failure_when_fingerprint_mismatches(self):
        from axiom.agents.pipeline.proof import (
            ProofSpec,
            ProofType,
            ReplayProofVerifier,
        )

        spec = ProofSpec(
            proof_type=ProofType.REPLAY,
            parameters={"expected_fingerprint": "0" * 64},
        )

        verifier = ReplayProofVerifier()
        result = verifier.verify(spec, step_outputs={"output_canonical": b"actual"})
        assert result.success is False

    def test_failure_when_output_missing(self):
        from axiom.agents.pipeline.proof import (
            ProofSpec,
            ProofType,
            ReplayProofVerifier,
        )

        spec = ProofSpec(
            proof_type=ProofType.REPLAY,
            parameters={"expected_fingerprint": "0" * 64},
        )
        verifier = ReplayProofVerifier()
        result = verifier.verify(spec, step_outputs={})
        assert result.success is False


# ---------------------------------------------------------------------------
# NullProofVerifier
# ---------------------------------------------------------------------------


class TestNullProofVerifier:
    def test_always_succeeds_with_reason(self):
        from axiom.agents.pipeline.proof import (
            NullProofVerifier,
            ProofSpec,
            ProofType,
        )

        spec = ProofSpec(
            proof_type=ProofType.NULL,
            parameters={"reason": "draft text; can't machine-verify"},
        )
        verifier = NullProofVerifier()
        result = verifier.verify(spec, step_outputs={})
        assert result.success is True
        assert "draft text" in result.rationale
        assert result.artifact is not None
        assert result.artifact.success is True

    def test_succeeds_even_without_reason(self):
        from axiom.agents.pipeline.proof import (
            NullProofVerifier,
            ProofSpec,
            ProofType,
        )

        verifier = NullProofVerifier()
        result = verifier.verify(
            ProofSpec(proof_type=ProofType.NULL),
            step_outputs={},
        )
        assert result.success is True


# ---------------------------------------------------------------------------
# Registry + dispatch
# ---------------------------------------------------------------------------


class TestProofVerifierRegistry:
    def test_default_registry_routes_each_proof_type(self, tmp_path):
        from axiom.agents.pipeline.proof import (
            ProofSpec,
            ProofType,
            StructuralAssertion,
            default_registry,
        )

        registry = default_registry()

        # null — easiest dispatch test
        spec = ProofSpec(proof_type=ProofType.NULL, parameters={"reason": "ok"})
        result = registry.verify(spec, step_outputs={})
        assert result.success is True

        # structural — file_exists w/ tmp_path
        target = tmp_path / "x.txt"
        target.write_text("y")
        spec_s = ProofSpec(
            proof_type=ProofType.STRUCTURAL,
            parameters={
                "assertions": [
                    StructuralAssertion(kind="file_exists", path=str(target), expected="")
                ]
            },
        )
        result_s = registry.verify(spec_s, step_outputs={})
        assert result_s.success is True

        # replay
        payload = b"x"
        fp = hashlib.sha256(payload).hexdigest()
        spec_r = ProofSpec(
            proof_type=ProofType.REPLAY,
            parameters={"expected_fingerprint": fp},
        )
        result_r = registry.verify(spec_r, step_outputs={"output_canonical": payload})
        assert result_r.success is True

        # retrieval
        spec_ret = ProofSpec(
            proof_type=ProofType.RETRIEVAL,
            parameters={
                "min_citations": 1,
                "min_score": 0.1,
                "recency_window_days": 365,
            },
        )
        result_ret = registry.verify(
            spec_ret,
            step_outputs={"citations": [{"score": 0.9, "recency_days": 1}]},
        )
        assert result_ret.success is True

    def test_unknown_proof_type_returns_failure(self):
        from axiom.agents.pipeline.proof import ProofSpec, ProofVerifierRegistry

        # bypass enum: registry must defensively handle a spec with no
        # registered verifier (e.g., if a future ProofType is added but the
        # registry hasn't been updated).
        registry = ProofVerifierRegistry()  # empty
        from axiom.agents.pipeline.proof import ProofType

        spec = ProofSpec(proof_type=ProofType.NULL)
        result = registry.verify(spec, step_outputs={})
        assert result.success is False
        assert "no verifier registered" in result.rationale

    def test_register_overwrites_existing(self):
        from axiom.agents.pipeline.proof import (
            ProofResult,
            ProofSpec,
            ProofType,
            ProofVerifierRegistry,
        )

        class StubVerifier:
            proof_type = ProofType.NULL

            def verify(self, spec, step_outputs):
                return ProofResult(success=False, artifact=None, rationale="stub")

        registry = ProofVerifierRegistry()
        registry.register(StubVerifier())
        result = registry.verify(
            ProofSpec(proof_type=ProofType.NULL), step_outputs={}
        )
        assert result.success is False
        assert result.rationale == "stub"


# ---------------------------------------------------------------------------
# artifact_id uniqueness
# ---------------------------------------------------------------------------


class TestArtifactIdUniqueness:
    def test_artifact_ids_are_unique_across_calls(self):
        from axiom.agents.pipeline.proof import (
            NullProofVerifier,
            ProofSpec,
            ProofType,
        )

        verifier = NullProofVerifier()
        ids = set()
        for _ in range(50):
            r = verifier.verify(
                ProofSpec(proof_type=ProofType.NULL), step_outputs={}
            )
            assert r.artifact is not None
            ids.add(r.artifact.artifact_id)
        assert len(ids) == 50


# ---------------------------------------------------------------------------
# Smoke: subprocess.CompletedProcess works as runner output
# ---------------------------------------------------------------------------


class TestRunnerCompatibility:
    def test_real_completed_process_shape(self):
        # Sanity check that our duck-typed _StubProcess matches the shape we
        # rely on (returncode, stdout, stderr) — same shape subprocess returns.
        from axiom.agents.pipeline.proof import (
            ProofSpec,
            ProofType,
            TestProofVerifier,
        )

        def runner(cmd: str) -> subprocess.CompletedProcess:
            return subprocess.CompletedProcess(
                args=cmd, returncode=0, stdout="ok", stderr=""
            )

        verifier = TestProofVerifier(runner=runner)
        result = verifier.verify(
            ProofSpec(
                proof_type=ProofType.TEST,
                parameters={"commands": ["echo hi"]},
            ),
            step_outputs={},
        )
        assert result.success is True
