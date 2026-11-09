# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tests for the axi review CLI."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

from click.testing import CliRunner

from axiom.extensions.builtins.review.cli import main
from axiom.extensions.builtins.review.tools.findings import Finding, FindingSet


def _fset(*severities: str) -> FindingSet:
    return FindingSet(findings=[
        Finding(
            severity=s,
            pass_kind="correctness",
            path="src/foo.py",
            line=10,
            message=f"test {s}",
        )
        for s in severities
    ])


# Minimal diff so local_diff mock has something to return.
FAKE_DIFF = (
    "diff --git a/src/foo.py b/src/foo.py\n"
    "+++ b/src/foo.py\n"
    "@@ -1,1 +1,2 @@\n"
    "-old\n"
    "+new\n"
)


class TestCLIExitCodes:
    def test_exit_0_when_no_blockers(self):
        runner = CliRunner()
        with patch("axiom.extensions.builtins.review.cli.local_diff", return_value=FAKE_DIFF), \
             patch("axiom.extensions.builtins.review.cli.RevUAgent") as MockAgent:
            mock_instance = MagicMock()
            mock_instance.review.return_value = _fset("minor", "nit")
            MockAgent.return_value = mock_instance

            result = runner.invoke(main, ["--base", "HEAD~1"])
            assert result.exit_code == 0

    def test_exit_1_when_blockers_exist(self):
        runner = CliRunner()
        with patch("axiom.extensions.builtins.review.cli.local_diff", return_value=FAKE_DIFF), \
             patch("axiom.extensions.builtins.review.cli.RevUAgent") as MockAgent:
            mock_instance = MagicMock()
            mock_instance.review.return_value = _fset("blocker", "minor")
            MockAgent.return_value = mock_instance

            result = runner.invoke(main, ["--base", "HEAD~1"])
            assert result.exit_code == 1


class TestCLIJsonOutput:
    def test_json_flag_emits_valid_json(self):
        runner = CliRunner()
        fset = _fset("minor")
        with patch("axiom.extensions.builtins.review.cli.local_diff", return_value=FAKE_DIFF), \
             patch("axiom.extensions.builtins.review.cli.RevUAgent") as MockAgent:
            mock_instance = MagicMock()
            mock_instance.review.return_value = fset
            MockAgent.return_value = mock_instance

            result = runner.invoke(main, ["--json", "--base", "HEAD~1"])
            assert result.exit_code == 0
            parsed = json.loads(result.output)
            assert isinstance(parsed, list)
            assert len(parsed) == 1
            assert parsed[0]["severity"] == "minor"


class TestCLINoValidator:
    def test_no_validator_flag_skips_validator(self):
        runner = CliRunner()
        with patch("axiom.extensions.builtins.review.cli.local_diff", return_value=FAKE_DIFF), \
             patch("axiom.extensions.builtins.review.cli.RevUAgent") as MockAgent:
            mock_instance = MagicMock()
            mock_instance.review.return_value = _fset("nit")
            MockAgent.return_value = mock_instance

            runner.invoke(main, ["--no-validator", "--base", "HEAD~1"])
            # Verify the agent was called with run_validator=False
            call_kwargs = mock_instance.review.call_args
            assert call_kwargs.kwargs.get("run_validator") is False
