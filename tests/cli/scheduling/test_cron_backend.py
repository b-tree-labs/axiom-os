# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tests for `axiom.cli.scheduling.cron.CronBackend` (issue #205).

CronBackend is the production case for any Linux/Unix host that uses
traditional cron. It installs/uninstalls via crontab editing with
a marker-comment for idempotence:

  */15 * * * * <command>  # axi schedule managed: <name>

Re-install matches the marker, removes the old line, appends the new
one. Uninstall removes the marker'd line, leaves the rest intact.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest


# ---------------------------------------------------------------------------
# Test double — captures all the runner activity for verification
# ---------------------------------------------------------------------------


@dataclass
class CapturedRun:
    command: Any
    input: str | None


@dataclass
class CapturedWrite:
    remote_path: str
    content: str


@dataclass
class MockRunner:
    host: str = "test-host"
    runs: list[CapturedRun] = field(default_factory=list)
    writes: list[CapturedWrite] = field(default_factory=list)
    # The mock's reply queue — caller scripts the (stdout, stderr, rc) per call.
    replies: list[tuple[str, str, int]] = field(default_factory=list)

    def run(self, command, *, input=None):
        from axiom.cli.scheduling.protocols import CompletedRun
        self.runs.append(CapturedRun(command=command, input=input))
        if self.replies:
            stdout, stderr, rc = self.replies.pop(0)
            return CompletedRun(stdout=stdout, stderr=stderr, returncode=rc)
        return CompletedRun(stdout="", stderr="", returncode=0)

    def write_file(self, remote_path, content):
        self.writes.append(CapturedWrite(remote_path=remote_path, content=content))


# ---------------------------------------------------------------------------
# Static shape
# ---------------------------------------------------------------------------


class TestStaticShape:
    def test_name_is_cron(self):
        from axiom.cli.scheduling.cron import CronBackend
        assert CronBackend().name == "cron"

    def test_artifact_filename_is_dot_cron(self):
        from axiom.cli.scheduling.cron import CronBackend
        assert CronBackend().artifact_filename("heartbeat") == "heartbeat.cron"


# ---------------------------------------------------------------------------
# Render
# ---------------------------------------------------------------------------


class TestRender:
    def test_render_includes_provenance_header(self):
        from axiom.cli.scheduling.cron import CronBackend
        out = CronBackend().render(
            schedule_name="heartbeat",
            cron="*/15 * * * *",
            command="bash ${REPO_DIR}/scripts/mo-heartbeat.sh",
        )
        assert "axi schedule" in out  # provenance hint
        assert "heartbeat" in out      # schedule name in header

    def test_render_includes_cron_line(self):
        from axiom.cli.scheduling.cron import CronBackend
        out = CronBackend().render(
            schedule_name="heartbeat",
            cron="*/15 * * * *",
            command="bash ${REPO_DIR}/scripts/mo-heartbeat.sh",
        )
        # Cron line ends with the marker so install/uninstall can find it.
        assert "*/15 * * * * bash ${REPO_DIR}/scripts/mo-heartbeat.sh" in out
        assert "# axi schedule managed: heartbeat" in out


# ---------------------------------------------------------------------------
# Install
# ---------------------------------------------------------------------------


class TestInstallFreshHost:
    """Installing onto a host that has no existing crontab for the user."""

    def test_install_writes_new_crontab_line(self):
        from axiom.cli.scheduling.cron import CronBackend
        backend = CronBackend()
        runner = MockRunner()
        # First call (crontab -l) → no existing crontab; cron returns
        # `no crontab for ...` on stderr with rc=1
        runner.replies = [("", "no crontab for user\n", 1)]

        backend.install(
            runner=runner,
            schedule_name="heartbeat",
            cron="*/15 * * * *",
            command="bash ${REPO_DIR}/scripts/mo-heartbeat.sh",
        )

        # Final invocation feeds the new crontab via stdin to `crontab -`.
        last = runner.runs[-1]
        assert last.command in (["crontab", "-"], "crontab -")
        assert last.input is not None
        # The new line is present
        assert "*/15 * * * *" in last.input
        assert "scripts/mo-heartbeat.sh" in last.input
        # With the marker
        assert "# axi schedule managed: heartbeat" in last.input

    def test_install_doesnt_blow_up_when_no_existing_crontab(self):
        from axiom.cli.scheduling.cron import CronBackend
        backend = CronBackend()
        runner = MockRunner()
        runner.replies = [("", "no crontab for user\n", 1)]

        backend.install(
            runner=runner,
            schedule_name="x",
            cron="0 * * * *",
            command="echo",
        )
        # Reached the install step
        assert any(
            c.command in (["crontab", "-"], "crontab -") for c in runner.runs
        )


class TestInstallExistingCrontab:
    """The host's user has an existing crontab. New entry is APPENDED
    without touching unrelated lines."""

    def test_appends_to_existing_crontab(self):
        from axiom.cli.scheduling.cron import CronBackend
        backend = CronBackend()
        runner = MockRunner()
        existing = (
            "# user's own job\n"
            "0 3 * * * /usr/local/bin/backup.sh\n"
        )
        runner.replies = [(existing, "", 0)]

        backend.install(
            runner=runner,
            schedule_name="heartbeat",
            cron="*/15 * * * *",
            command="bash ${REPO_DIR}/scripts/mo-heartbeat.sh",
        )

        new_crontab = runner.runs[-1].input
        assert "user's own job" in new_crontab
        assert "/usr/local/bin/backup.sh" in new_crontab
        assert "# axi schedule managed: heartbeat" in new_crontab


class TestReinstallIdempotence:
    """Re-installing the same schedule replaces the existing marker'd
    line. No duplicates."""

    def test_reinstall_replaces_old_line(self):
        from axiom.cli.scheduling.cron import CronBackend
        backend = CronBackend()
        runner = MockRunner()
        existing = (
            "# user's own job\n"
            "0 3 * * * /usr/local/bin/backup.sh\n"
            "0 * * * * old_command  # axi schedule managed: heartbeat\n"
        )
        runner.replies = [(existing, "", 0)]

        backend.install(
            runner=runner,
            schedule_name="heartbeat",
            cron="*/15 * * * *",
            command="bash ${REPO_DIR}/scripts/mo-heartbeat.sh",
        )

        new_crontab = runner.runs[-1].input
        # Old `heartbeat` line gone
        assert "old_command" not in new_crontab
        # New line present
        assert "*/15 * * * *" in new_crontab
        assert "scripts/mo-heartbeat.sh" in new_crontab
        # Only ONE line for the heartbeat marker
        assert new_crontab.count("# axi schedule managed: heartbeat") == 1
        # Unrelated user job preserved
        assert "/usr/local/bin/backup.sh" in new_crontab

    def test_reinstall_with_same_definition_is_unchanged(self):
        """Idempotent: if the new line is identical to the existing one,
        the crontab is still re-written but the content matches."""
        from axiom.cli.scheduling.cron import CronBackend
        backend = CronBackend()
        runner = MockRunner()
        existing = (
            "*/15 * * * * bash ${REPO_DIR}/scripts/mo-heartbeat.sh  "
            "# axi schedule managed: heartbeat\n"
        )
        runner.replies = [(existing, "", 0)]

        backend.install(
            runner=runner,
            schedule_name="heartbeat",
            cron="*/15 * * * *",
            command="bash ${REPO_DIR}/scripts/mo-heartbeat.sh",
        )

        new_crontab = runner.runs[-1].input
        assert new_crontab.count("# axi schedule managed: heartbeat") == 1


# ---------------------------------------------------------------------------
# Uninstall
# ---------------------------------------------------------------------------


class TestUninstall:
    def test_removes_only_the_marker_line(self):
        from axiom.cli.scheduling.cron import CronBackend
        backend = CronBackend()
        runner = MockRunner()
        existing = (
            "# user's own job\n"
            "0 3 * * * /usr/local/bin/backup.sh\n"
            "*/15 * * * * heartbeat_cmd  # axi schedule managed: heartbeat\n"
            "0 * * * * other_cmd  # axi schedule managed: other-schedule\n"
        )
        runner.replies = [(existing, "", 0)]

        backend.uninstall(runner=runner, schedule_name="heartbeat")

        new_crontab = runner.runs[-1].input
        assert "heartbeat_cmd" not in new_crontab
        assert "/usr/local/bin/backup.sh" in new_crontab
        assert "other_cmd" in new_crontab  # other schedule untouched

    def test_uninstall_unknown_is_noop_not_failure(self):
        from axiom.cli.scheduling.cron import CronBackend
        backend = CronBackend()
        runner = MockRunner()
        existing = "0 3 * * * /usr/local/bin/backup.sh\n"
        runner.replies = [(existing, "", 0)]

        # Should not raise
        backend.uninstall(runner=runner, schedule_name="never-installed")

        new_crontab = runner.runs[-1].input
        # User's crontab preserved unchanged
        assert "/usr/local/bin/backup.sh" in new_crontab

    def test_uninstall_when_no_crontab_is_noop(self):
        from axiom.cli.scheduling.cron import CronBackend
        backend = CronBackend()
        runner = MockRunner()
        runner.replies = [("", "no crontab for user\n", 1)]

        # No raise
        backend.uninstall(runner=runner, schedule_name="anything")
