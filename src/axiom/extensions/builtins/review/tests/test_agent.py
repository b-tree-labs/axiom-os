# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tests for the RevUAgent."""

from __future__ import annotations

import json
from dataclasses import dataclass
from unittest.mock import patch

from axiom.extensions.builtins.review.agents.rev_u.agent import RevUAgent


@dataclass
class _FakeResponse:
    text: str
    success: bool = True


class FakeLLM:
    """Returns a single minor finding per call."""

    def __init__(self, path="src/foo.py", line=10):
        self._path = path
        self._line = line
        self.call_count = 0

    def complete(self, prompt: str, system: str = "") -> _FakeResponse:
        self.call_count += 1
        return _FakeResponse(text=json.dumps([{
            "severity": "minor",
            "path": self._path,
            "line": self._line,
            "message": "test finding",
            "suggested_fix": None,
        }]))


# A diff that touches src/foo.py around line 10.
SAMPLE_DIFF = (
    "diff --git a/src/foo.py b/src/foo.py\n"
    "--- a/src/foo.py\n"
    "+++ b/src/foo.py\n"
    "@@ -8,5 +8,7 @@ def foo():\n"
    " a\n"
    " b\n"
    "+new_10\n"
    "+new_11\n"
    " c\n"
    " d\n"
)


class TestRevUAgentRunsAllPasses:
    def test_runs_all_5_passes_by_default(self):
        llm = FakeLLM()
        agent = RevUAgent(llm=llm)
        with patch(
            "axiom.extensions.builtins.review.tools.context.gather_context",
            return_value=({"src/foo.py": "def foo(): pass"}, []),
        ):
            fset = agent.review(SAMPLE_DIFF, run_validator=False)
        # 5 passes × 1 finding each = 5 findings total
        assert llm.call_count == 5
        assert len(fset) == 5

    def test_single_pass_filter(self):
        llm = FakeLLM()
        agent = RevUAgent(llm=llm)
        with patch(
            "axiom.extensions.builtins.review.tools.context.gather_context",
            return_value=({"src/foo.py": "def foo(): pass"}, []),
        ):
            fset = agent.review(SAMPLE_DIFF, passes=["security"], run_validator=False)
        assert llm.call_count == 1
        for f in fset:
            assert f.pass_kind == "security"

    def test_aggregates_findings_across_passes(self):
        llm = FakeLLM()
        agent = RevUAgent(llm=llm)
        with patch(
            "axiom.extensions.builtins.review.tools.context.gather_context",
            return_value=({}, []),
        ):
            fset = agent.review(SAMPLE_DIFF, passes=["correctness", "docs"], run_validator=False)
        pass_kinds = {f.pass_kind for f in fset}
        assert "correctness" in pass_kinds
        assert "docs" in pass_kinds

    def test_exception_in_pass_is_logged_and_skipped(self):
        """A pass that raises should be caught; other passes still run."""
        llm = FakeLLM()
        agent = RevUAgent(llm=llm)

        with patch(
            "axiom.extensions.builtins.review.tools.context.gather_context",
            return_value=({}, []),
        ), patch(
            "axiom.extensions.builtins.review.agents.rev_u.passes.correctness.run",
            side_effect=RuntimeError("boom"),
        ):
            # security and correctness — correctness raises, security should still run
            fset = agent.review(
                SAMPLE_DIFF,
                passes=["correctness", "security"],
                run_validator=False,
            )
        # correctness raised, security succeeded → 1 finding from security
        security_findings = [f for f in fset if f.pass_kind == "security"]
        assert len(security_findings) == 1
