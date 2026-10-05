# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tests for the five review passes using a FakeLLM."""

from __future__ import annotations

import json
from dataclasses import dataclass

from axiom.extensions.builtins.review.agents.rev_u.passes import (
    correctness,
    docs,
    performance,
    security,
)
from axiom.extensions.builtins.review.agents.rev_u.passes import tests as tests_pass
from axiom.extensions.builtins.review.tools.findings import Finding


@dataclass
class _FakeResponse:
    text: str
    success: bool = True


class FakeLLM:
    """Test double for the LLM gateway.  Returns canned JSON findings."""

    def __init__(self, findings_json: str = "[]", system_prompt_capture: list | None = None):
        self._findings_json = findings_json
        self._captured_system: list[str] = system_prompt_capture if system_prompt_capture is not None else []

    def complete(self, prompt: str, system: str = "") -> _FakeResponse:
        self._captured_system.append(system)
        return _FakeResponse(text=self._findings_json)


def _canned_finding(pass_kind: str) -> str:
    return json.dumps([{
        "severity": "minor",
        "path": "src/foo.py",
        "line": 10,
        "message": f"test {pass_kind} finding",
        "suggested_fix": None,
    }])


DIFF = (
    "diff --git a/src/foo.py b/src/foo.py\n"
    "+++ b/src/foo.py\n"
    "@@ -9,3 +9,5 @@\n"
    " # context\n"
    "+new_line_10\n"
    "+new_line_11\n"
)
CTX: dict[str, str] = {"src/foo.py": "def foo():\n    pass\n"}


class TestCorrectnessPass:
    def test_returns_findings_with_correct_pass_kind(self):
        llm = FakeLLM(_canned_finding("correctness"))
        findings = correctness.run(DIFF, CTX, llm)
        assert len(findings) == 1
        assert findings[0].pass_kind == "correctness"
        assert isinstance(findings[0], Finding)

    def test_system_prompt_contains_pass_keywords(self):
        captured: list[str] = []
        llm = FakeLLM("[]", system_prompt_capture=captured)
        correctness.run(DIFF, CTX, llm)
        assert len(captured) == 1
        prompt = captured[0].lower()
        assert "logic" in prompt or "invariant" in prompt or "type" in prompt


class TestPerformancePass:
    def test_returns_findings_with_correct_pass_kind(self):
        llm = FakeLLM(_canned_finding("performance"))
        findings = performance.run(DIFF, CTX, llm)
        assert len(findings) == 1
        assert findings[0].pass_kind == "performance"

    def test_system_prompt_contains_pass_keywords(self):
        captured: list[str] = []
        llm = FakeLLM("[]", system_prompt_capture=captured)
        performance.run(DIFF, CTX, llm)
        prompt = captured[0].lower()
        assert "n+1" in prompt or "complexity" in prompt or "allocation" in prompt


class TestSecurityPass:
    def test_returns_findings_with_correct_pass_kind(self):
        llm = FakeLLM(_canned_finding("security"))
        findings = security.run(DIFF, CTX, llm)
        assert len(findings) == 1
        assert findings[0].pass_kind == "security"

    def test_system_prompt_contains_pass_keywords(self):
        captured: list[str] = []
        llm = FakeLLM("[]", system_prompt_capture=captured)
        security.run(DIFF, CTX, llm)
        prompt = captured[0].lower()
        assert "injection" in prompt
        assert "secrets" in prompt or "credential" in prompt


class TestDocsPass:
    def test_returns_findings_with_correct_pass_kind(self):
        llm = FakeLLM(_canned_finding("docs"))
        findings = docs.run(DIFF, CTX, llm)
        assert len(findings) == 1
        assert findings[0].pass_kind == "docs"

    def test_system_prompt_contains_pass_keywords(self):
        captured: list[str] = []
        llm = FakeLLM("[]", system_prompt_capture=captured)
        docs.run(DIFF, CTX, llm)
        prompt = captured[0].lower()
        assert "docstring" in prompt or "documentation" in prompt


class TestTestsPass:
    def test_returns_findings_with_correct_pass_kind(self):
        llm = FakeLLM(_canned_finding("tests"))
        findings = tests_pass.run(DIFF, CTX, llm)
        assert len(findings) == 1
        assert findings[0].pass_kind == "tests"

    def test_system_prompt_contains_pass_keywords(self):
        captured: list[str] = []
        llm = FakeLLM("[]", system_prompt_capture=captured)
        tests_pass.run(DIFF, CTX, llm)
        prompt = captured[0].lower()
        assert "coverage" in prompt or "test" in prompt
