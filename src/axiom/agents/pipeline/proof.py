# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""ProofSpec types + verifier harness for proof-bound plan steps (ADR-034 §D2/§D9).

Per ADR-034 every PlanStep declares a `proof:` field that names what would
constitute success. Per analysis §7.6 there are seven proof shapes:
test, typecheck, structural, retrieval, attestation, replay, null.

This module is self-contained — no dependencies on plan.py / agent.py. Other
tracks import `ProofSpec` and `ProofResult` from here.
"""

from __future__ import annotations

import ast
import hashlib
import shlex
import subprocess
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Literal, Protocol, runtime_checkable

# ---------------------------------------------------------------------------
# Core types
# ---------------------------------------------------------------------------


class ProofType(str, Enum):
    """Enumerated proof shapes per ADR-034 §D9 + analysis §7.6."""

    TEST = "test"
    TYPECHECK = "typecheck"
    STRUCTURAL = "structural"
    RETRIEVAL = "retrieval"
    ATTESTATION = "attestation"
    REPLAY = "replay"
    NULL = "null"


@dataclass(frozen=True)
class ProofSpec:
    """Declares what would constitute success for a plan step."""

    proof_type: ProofType
    parameters: Mapping[str, Any] = field(default_factory=dict)
    description: str = ""


@dataclass(frozen=True)
class StructuralAssertion:
    """Single structural claim about a source artifact."""

    kind: Literal["file_exists", "function_signature", "class_attribute"]
    path: str
    expected: str


@dataclass(frozen=True)
class ProofArtifact:
    """Evidence produced when a proof was attempted."""

    spec: ProofSpec
    artifact_id: str
    captured_outputs: Mapping[str, Any]
    success: bool
    rationale: str


@dataclass(frozen=True)
class ProofResult:
    """Outcome of a verifier.verify() call."""

    success: bool
    artifact: ProofArtifact | None
    rationale: str


@runtime_checkable
class ProofVerifier(Protocol):
    """Protocol every concrete verifier conforms to."""

    proof_type: ProofType

    def verify(
        self, spec: ProofSpec, step_outputs: Mapping[str, Any]
    ) -> ProofResult: ...


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _new_artifact_id() -> str:
    """Auto-generate a fragment-id-shaped opaque artifact id (per CLAUDE.md)."""
    return uuid.uuid4().hex


def _make_artifact(
    spec: ProofSpec,
    captured: Mapping[str, Any],
    success: bool,
    rationale: str,
) -> ProofArtifact:
    return ProofArtifact(
        spec=spec,
        artifact_id=_new_artifact_id(),
        captured_outputs=dict(captured),
        success=success,
        rationale=rationale,
    )


def _missing_param(spec: ProofSpec, name: str) -> ProofResult:
    rationale = f"missing required parameter '{name}' for {spec.proof_type.value} proof"
    artifact = _make_artifact(spec, {}, False, rationale)
    return ProofResult(success=False, artifact=artifact, rationale=rationale)


# ---------------------------------------------------------------------------
# Default subprocess runner
# ---------------------------------------------------------------------------


def _default_subprocess_runner(cmd: str) -> subprocess.CompletedProcess[str]:
    """Default runner for shell-out verifiers. Constructed lazily, never at import."""
    return subprocess.run(
        shlex.split(cmd),
        capture_output=True,
        text=True,
        check=False,
    )


# ---------------------------------------------------------------------------
# TestProofVerifier
# ---------------------------------------------------------------------------


class TestProofVerifier:
    """Runs declared shell commands; success when every exit code is zero."""

    proof_type = ProofType.TEST

    def __init__(self, runner: Callable[[str], Any] | None = None) -> None:
        self.runner = runner or _default_subprocess_runner

    def verify(
        self, spec: ProofSpec, step_outputs: Mapping[str, Any]
    ) -> ProofResult:
        commands = spec.parameters.get("commands")
        if not commands:
            return _missing_param(spec, "commands")

        exit_codes: list[int] = []
        stdouts: list[str] = []
        stderrs: list[str] = []
        for cmd in commands:
            proc = self.runner(cmd)
            exit_codes.append(getattr(proc, "returncode", 1))
            stdouts.append(getattr(proc, "stdout", "") or "")
            stderrs.append(getattr(proc, "stderr", "") or "")

        success = all(code == 0 for code in exit_codes)
        rationale = (
            "all commands exited 0"
            if success
            else f"command(s) failed: exit codes {exit_codes}"
        )
        captured = {
            "commands": list(commands),
            "exit_codes": exit_codes,
            "stdout": stdouts,
            "stderr": stderrs,
        }
        artifact = _make_artifact(spec, captured, success, rationale)
        return ProofResult(success=success, artifact=artifact, rationale=rationale)


# ---------------------------------------------------------------------------
# TypecheckProofVerifier
# ---------------------------------------------------------------------------


class TypecheckProofVerifier:
    """Invokes a typechecker (mypy / ruff) over declared paths via injected runner."""

    proof_type = ProofType.TYPECHECK

    def __init__(
        self,
        runner: Callable[[str], Any] | None = None,
        tool: str = "mypy",
    ) -> None:
        self.runner = runner or _default_subprocess_runner
        self.tool = tool

    def verify(
        self, spec: ProofSpec, step_outputs: Mapping[str, Any]
    ) -> ProofResult:
        paths = spec.parameters.get("paths")
        if not paths:
            return _missing_param(spec, "paths")

        tool = spec.parameters.get("tool", self.tool)
        cmd = f"{tool} {' '.join(paths)}"
        proc = self.runner(cmd)
        exit_code = getattr(proc, "returncode", 1)
        stdout = getattr(proc, "stdout", "") or ""
        stderr = getattr(proc, "stderr", "") or ""

        success = exit_code == 0
        rationale = (
            f"{tool} clean on {len(paths)} path(s)"
            if success
            else f"{tool} reported errors (exit {exit_code})"
        )
        captured = {
            "tool": tool,
            "paths": list(paths),
            "exit_code": exit_code,
            "stdout": stdout,
            "stderr": stderr,
        }
        artifact = _make_artifact(spec, captured, success, rationale)
        return ProofResult(success=success, artifact=artifact, rationale=rationale)


# ---------------------------------------------------------------------------
# StructuralProofVerifier
# ---------------------------------------------------------------------------


class StructuralProofVerifier:
    """Pure-Python checks: file_exists, function_signature, class_attribute."""

    proof_type = ProofType.STRUCTURAL

    def verify(
        self, spec: ProofSpec, step_outputs: Mapping[str, Any]
    ) -> ProofResult:
        assertions = spec.parameters.get("assertions")
        if not assertions:
            return _missing_param(spec, "assertions")

        results: list[dict[str, Any]] = []
        for assertion in assertions:
            ok, detail = self._check(assertion)
            results.append(
                {
                    "kind": assertion.kind,
                    "path": assertion.path,
                    "expected": assertion.expected,
                    "passed": ok,
                    "detail": detail,
                }
            )

        success = all(r["passed"] for r in results)
        rationale = (
            f"{len(results)} structural assertion(s) passed"
            if success
            else "; ".join(
                f"{r['path']}:{r['kind']} -> {r['detail']}"
                for r in results
                if not r["passed"]
            )
        )
        captured = {"results": results}
        artifact = _make_artifact(spec, captured, success, rationale)
        return ProofResult(success=success, artifact=artifact, rationale=rationale)

    def _check(self, assertion: StructuralAssertion) -> tuple[bool, str]:
        if assertion.kind == "file_exists":
            exists = Path(assertion.path).exists()
            return exists, "file present" if exists else f"missing: {assertion.path}"
        if assertion.kind == "function_signature":
            return self._check_function_signature(assertion)
        if assertion.kind == "class_attribute":
            return self._check_class_attribute(assertion)
        return False, f"unknown assertion kind: {assertion.kind}"

    def _check_function_signature(
        self, assertion: StructuralAssertion
    ) -> tuple[bool, str]:
        # expected format: "func_name(arg1, arg2, ...)"
        try:
            source = Path(assertion.path).read_text()
        except OSError as e:
            return False, f"could not read {assertion.path}: {e}"
        try:
            tree = ast.parse(source)
        except SyntaxError as e:
            return False, f"parse error: {e}"

        func_name, _, rest = assertion.expected.partition("(")
        func_name = func_name.strip()
        expected_args = [
            a.strip() for a in rest.rstrip(")").split(",") if a.strip()
        ]

        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if node.name == func_name:
                    actual_args = [a.arg for a in node.args.args]
                    if actual_args == expected_args:
                        return True, f"signature matches: {assertion.expected}"
                    return (
                        False,
                        f"signature mismatch: actual={actual_args} expected={expected_args}",
                    )
        return False, f"function '{func_name}' not found in {assertion.path}"

    def _check_class_attribute(
        self, assertion: StructuralAssertion
    ) -> tuple[bool, str]:
        # expected format: "ClassName.attr_name"
        try:
            source = Path(assertion.path).read_text()
        except OSError as e:
            return False, f"could not read {assertion.path}: {e}"
        try:
            tree = ast.parse(source)
        except SyntaxError as e:
            return False, f"parse error: {e}"

        class_name, _, attr = assertion.expected.partition(".")
        class_name = class_name.strip()
        attr = attr.strip()
        if not attr:
            return False, "expected format 'ClassName.attr_name'"

        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef) and node.name == class_name:
                for stmt in node.body:
                    if isinstance(stmt, ast.AnnAssign) and isinstance(
                        stmt.target, ast.Name
                    ):
                        if stmt.target.id == attr:
                            return True, f"{assertion.expected} present"
                    elif isinstance(stmt, ast.Assign):
                        for target in stmt.targets:
                            if isinstance(target, ast.Name) and target.id == attr:
                                return True, f"{assertion.expected} present"
                    elif isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        if stmt.name == attr:
                            return True, f"{assertion.expected} present (method)"
                return (
                    False,
                    f"class '{class_name}' present but attribute '{attr}' missing",
                )
        return False, f"class '{class_name}' not found in {assertion.path}"


# ---------------------------------------------------------------------------
# RetrievalProofVerifier
# ---------------------------------------------------------------------------


class RetrievalProofVerifier:
    """Checks that step_outputs.citations meet count + score + recency thresholds."""

    proof_type = ProofType.RETRIEVAL

    def verify(
        self, spec: ProofSpec, step_outputs: Mapping[str, Any]
    ) -> ProofResult:
        min_citations = int(spec.parameters.get("min_citations", 1))
        min_score = float(spec.parameters.get("min_score", 0.0))
        recency_window_days = int(
            spec.parameters.get("recency_window_days", 365)
        )

        citations = step_outputs.get("citations")
        if citations is None:
            rationale = "step_outputs missing 'citations'"
            artifact = _make_artifact(spec, {}, False, rationale)
            return ProofResult(success=False, artifact=artifact, rationale=rationale)

        passing = [
            c
            for c in citations
            if float(c.get("score", 0.0)) >= min_score
            and int(c.get("recency_days", 10**9)) <= recency_window_days
        ]
        success = len(passing) >= min_citations
        rationale = (
            f"{len(passing)}/{len(citations)} citations meet thresholds"
            f" (need {min_citations})"
        )
        captured = {
            "min_citations": min_citations,
            "min_score": min_score,
            "recency_window_days": recency_window_days,
            "passing_count": len(passing),
            "total_count": len(citations),
        }
        artifact = _make_artifact(spec, captured, success, rationale)
        return ProofResult(success=success, artifact=artifact, rationale=rationale)


# ---------------------------------------------------------------------------
# AttestationProofVerifier
# ---------------------------------------------------------------------------


class AttestationProofVerifier:
    """Checks attestation set + signatures via injected signature_verifier."""

    proof_type = ProofType.ATTESTATION

    def __init__(
        self,
        signature_verifier: Callable[[str, str], bool] | None = None,
    ) -> None:
        # default: trust all signatures (callers should inject for production)
        self.signature_verifier = signature_verifier or (lambda p, s: True)

    def verify(
        self, spec: ProofSpec, step_outputs: Mapping[str, Any]
    ) -> ProofResult:
        required: Sequence[str] = spec.parameters.get("required_attesters", [])
        min_attesters = int(spec.parameters.get("min_attesters", 1))
        attestations = step_outputs.get("attestations")

        if attestations is None:
            rationale = "step_outputs missing 'attestations'"
            artifact = _make_artifact(spec, {}, False, rationale)
            return ProofResult(success=False, artifact=artifact, rationale=rationale)

        verified: list[dict[str, Any]] = []
        for att in attestations:
            principal = att.get("principal_id", "")
            signature = att.get("signature", "")
            ok = bool(self.signature_verifier(principal, signature))
            verified.append({"principal_id": principal, "signature_ok": ok})

        valid_principals = {v["principal_id"] for v in verified if v["signature_ok"]}
        missing_required = [p for p in required if p not in valid_principals]

        if missing_required:
            success = False
            rationale = f"missing required attester(s): {missing_required}"
        elif len(valid_principals) < min_attesters:
            success = False
            rationale = (
                f"only {len(valid_principals)} valid attester(s); need {min_attesters}"
            )
        else:
            success = True
            rationale = (
                f"{len(valid_principals)} valid attester(s); all required present"
            )

        captured = {
            "required_attesters": list(required),
            "min_attesters": min_attesters,
            "verified": verified,
            "valid_count": len(valid_principals),
        }
        artifact = _make_artifact(spec, captured, success, rationale)
        return ProofResult(success=success, artifact=artifact, rationale=rationale)


# ---------------------------------------------------------------------------
# ReplayProofVerifier
# ---------------------------------------------------------------------------


class ReplayProofVerifier:
    """Hashes step_outputs.output_canonical and compares to expected_fingerprint."""

    proof_type = ProofType.REPLAY

    def verify(
        self, spec: ProofSpec, step_outputs: Mapping[str, Any]
    ) -> ProofResult:
        expected = spec.parameters.get("expected_fingerprint")
        if not expected:
            return _missing_param(spec, "expected_fingerprint")

        payload = step_outputs.get("output_canonical")
        if payload is None:
            rationale = "step_outputs missing 'output_canonical'"
            artifact = _make_artifact(spec, {}, False, rationale)
            return ProofResult(success=False, artifact=artifact, rationale=rationale)

        actual = hashlib.sha256(payload).hexdigest()
        success = actual == expected
        rationale = (
            "fingerprint matches"
            if success
            else f"fingerprint mismatch: actual={actual[:12]}... expected={str(expected)[:12]}..."
        )
        captured = {"expected_fingerprint": expected, "actual_fingerprint": actual}
        artifact = _make_artifact(spec, captured, success, rationale)
        return ProofResult(success=success, artifact=artifact, rationale=rationale)


# ---------------------------------------------------------------------------
# NullProofVerifier
# ---------------------------------------------------------------------------


class NullProofVerifier:
    """Explicit accept-on-trust; audit-visible. Always succeeds."""

    proof_type = ProofType.NULL

    def verify(
        self, spec: ProofSpec, step_outputs: Mapping[str, Any]
    ) -> ProofResult:
        reason = spec.parameters.get("reason", "no reason supplied")
        rationale = f"null proof accepted on trust: {reason}"
        captured = {"reason": reason}
        artifact = _make_artifact(spec, captured, True, rationale)
        return ProofResult(success=True, artifact=artifact, rationale=rationale)


# ---------------------------------------------------------------------------
# Registry + dispatch
# ---------------------------------------------------------------------------


class ProofVerifierRegistry:
    """Routes a ProofSpec to its registered verifier by ProofType."""

    def __init__(self) -> None:
        self._verifiers: dict[ProofType, ProofVerifier] = {}

    def register(self, verifier: ProofVerifier) -> None:
        self._verifiers[verifier.proof_type] = verifier

    def get(self, proof_type: ProofType) -> ProofVerifier | None:
        return self._verifiers.get(proof_type)

    def verify(
        self, spec: ProofSpec, step_outputs: Mapping[str, Any]
    ) -> ProofResult:
        verifier = self._verifiers.get(spec.proof_type)
        if verifier is None:
            rationale = (
                f"no verifier registered for type {spec.proof_type.value!r}"
            )
            return ProofResult(success=False, artifact=None, rationale=rationale)
        return verifier.verify(spec, step_outputs)


def default_registry() -> ProofVerifierRegistry:
    """Return a registry with all built-in verifiers registered."""
    registry = ProofVerifierRegistry()
    registry.register(TestProofVerifier())
    registry.register(TypecheckProofVerifier())
    registry.register(StructuralProofVerifier())
    registry.register(RetrievalProofVerifier())
    registry.register(AttestationProofVerifier())
    registry.register(ReplayProofVerifier())
    registry.register(NullProofVerifier())
    return registry


__all__ = [
    "AttestationProofVerifier",
    "NullProofVerifier",
    "ProofArtifact",
    "ProofResult",
    "ProofSpec",
    "ProofType",
    "ProofVerifier",
    "ProofVerifierRegistry",
    "ReplayProofVerifier",
    "RetrievalProofVerifier",
    "StructuralAssertion",
    "StructuralProofVerifier",
    "TestProofVerifier",
    "TypecheckProofVerifier",
    "default_registry",
]
