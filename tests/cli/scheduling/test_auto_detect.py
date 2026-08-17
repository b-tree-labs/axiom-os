# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tests for `axiom.cli.scheduling.detect.detect_backend` (issue #205, slice 13).

Auto-selection logic the CLI runs when `--backend auto` (or no
`--backend` flag) is passed:

  Darwin   → launchd
  Linux + systemctl available → systemd
  Linux without systemctl      → cron
  Windows (uname fails, `ver` reports MS Windows) → wintasks
  Unknown                       → cron (fallback) + warning

The probe is a chain of runner calls: try `uname -s`, then probe for
systemctl when needed, fall through to a Windows `ver` probe when
uname is unavailable.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest


@dataclass
class CapturedRun:
    command: Any
    input: str | None


@dataclass
class MockRunner:
    host: str = "probe-test"
    runs: list[CapturedRun] = field(default_factory=list)
    replies: list[tuple[str, str, int]] = field(default_factory=list)

    def run(self, command, *, input=None):
        from axiom.cli.scheduling.protocols import CompletedRun
        self.runs.append(CapturedRun(command=command, input=input))
        if self.replies:
            stdout, stderr, rc = self.replies.pop(0)
            return CompletedRun(stdout=stdout, stderr=stderr, returncode=rc)
        return CompletedRun(stdout="", stderr="", returncode=0)

    def write_file(self, remote_path, content):  # noqa: D401
        """Unused — detection is read-only."""
        raise AssertionError("detect_backend should never write")


class TestDarwin:
    def test_darwin_uname_picks_launchd(self):
        from axiom.cli.scheduling.detect import detect_backend
        runner = MockRunner()
        runner.replies = [("Darwin\n", "", 0)]
        assert detect_backend(runner) == "launchd"


class TestLinux:
    def test_linux_with_systemctl_picks_systemd(self):
        from axiom.cli.scheduling.detect import detect_backend
        runner = MockRunner()
        runner.replies = [
            ("Linux\n", "", 0),
            ("/usr/bin/systemctl\n", "", 0),  # command -v systemctl
        ]
        assert detect_backend(runner) == "systemd"

    def test_linux_without_systemctl_picks_cron(self):
        from axiom.cli.scheduling.detect import detect_backend
        runner = MockRunner()
        runner.replies = [
            ("Linux\n", "", 0),
            ("", "not found", 1),  # command -v systemctl fails
        ]
        assert detect_backend(runner) == "cron"


class TestWindows:
    def test_uname_fails_then_ver_picks_wintasks(self):
        """OpenSSH on Windows defaults to cmd.exe — `uname` isn't on PATH.
        Fall through to `ver`, which returns 'Microsoft Windows ...'."""
        from axiom.cli.scheduling.detect import detect_backend
        runner = MockRunner()
        runner.replies = [
            ("", "'uname' is not recognized\n", 1),  # uname fails
            ("Microsoft Windows [Version 10.0.22631.3593]\r\n", "", 0),  # ver
        ]
        assert detect_backend(runner) == "wintasks"

    def test_uname_succeeds_with_mingw_picks_wintasks(self):
        """Git Bash / MSYS exposes uname; their output contains MINGW or
        MSYS so we still route to wintasks (Task Scheduler), not cron."""
        from axiom.cli.scheduling.detect import detect_backend
        runner = MockRunner()
        runner.replies = [("MINGW64_NT-10.0\n", "", 0)]
        assert detect_backend(runner) == "wintasks"


class TestFallback:
    def test_both_probes_fail_falls_back_to_cron(self):
        from axiom.cli.scheduling.detect import detect_backend
        runner = MockRunner()
        runner.replies = [
            ("", "", 127),  # uname fails
            ("", "", 127),  # ver fails
        ]
        # Unknown — cron is the universal POSIX fallback
        assert detect_backend(runner) == "cron"

    def test_unrecognized_uname_falls_back_to_cron(self):
        from axiom.cli.scheduling.detect import detect_backend
        runner = MockRunner()
        runner.replies = [("HP-UX\n", "", 0)]
        # Don't have an HP-UX backend; cron is the safest POSIX guess
        assert detect_backend(runner) == "cron"


class TestProbeShape:
    def test_uname_is_the_first_call(self):
        from axiom.cli.scheduling.detect import detect_backend
        runner = MockRunner()
        runner.replies = [("Darwin\n", "", 0)]
        detect_backend(runner)
        assert "uname" in str(runner.runs[0].command).lower()
