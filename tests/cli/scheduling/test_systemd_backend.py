# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tests for `axiom.cli.scheduling.systemd.SystemdTimerBackend` (issue #205, slice 10).

SystemdTimerBackend is the third SchedulerBackend impl. Modern Linux
distributions (Ubuntu 22+, Fedora, Arch, openSUSE) prefer
`systemd --user` timers over crontab — they get journald logs, retry
semantics, calendar specs that don't fight DST, and a clean
disable/enable lifecycle.

Each schedule needs *two* unit files: a `.service` (what to run) and a
`.timer` (when to run). The backend's artifact bundles both into a
single `.systemd` file with `=== service ===` / `=== timer ===`
delimiters so the existing `deploy/<host>/<name>.<ext>` discovery
pattern still works (one file per schedule).

Install lifecycle (per schedule):

  1. resolve user systemd dir (~/.config/systemd/user)
  2. write <dir>/<name>.service
  3. write <dir>/<name>.timer
  4. systemctl --user daemon-reload
  5. systemctl --user enable --now <name>.timer
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest


# ---------------------------------------------------------------------------
# MockRunner (same shape as test_cron_backend.py / test_launchd_backend.py)
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


def _probe_replies(*, user_dir="/home/test/.config/systemd/user"):
    """Default probe queue for resolve_user_dir."""
    return [(user_dir + "\n", "", 0)]


# ---------------------------------------------------------------------------
# Static shape
# ---------------------------------------------------------------------------


class TestStaticShape:
    def test_conforms_to_scheduler_backend(self):
        from axiom.cli.scheduling.protocols import SchedulerBackend
        from axiom.cli.scheduling.systemd import SystemdTimerBackend
        assert isinstance(SystemdTimerBackend(), SchedulerBackend)

    def test_name_is_systemd(self):
        from axiom.cli.scheduling.systemd import SystemdTimerBackend
        assert SystemdTimerBackend().name == "systemd"

    def test_artifact_filename_is_dot_systemd(self):
        from axiom.cli.scheduling.systemd import SystemdTimerBackend
        assert SystemdTimerBackend().artifact_filename("heartbeat") == "heartbeat.systemd"


# ---------------------------------------------------------------------------
# Render
# ---------------------------------------------------------------------------


class TestRender:
    def test_render_has_both_service_and_timer_sections(self):
        from axiom.cli.scheduling.systemd import SystemdTimerBackend
        out = SystemdTimerBackend().render(
            schedule_name="heartbeat",
            cron="*/15 * * * *",
            command="echo hi",
        )
        assert "=== service ===" in out
        assert "=== timer ===" in out

    def test_service_section_contains_execstart(self):
        from axiom.cli.scheduling.systemd import SystemdTimerBackend
        out = SystemdTimerBackend().render(
            schedule_name="x",
            cron="0 12 * * *",
            command="echo $HOME",
        )
        # service section between service/timer markers
        svc = out.split("=== service ===")[1].split("=== timer ===")[0]
        assert "[Service]" in svc
        assert "Type=oneshot" in svc
        # Command wrapped in sh -c so shell metachars resolve
        assert "/bin/sh -c" in svc
        assert "echo $HOME" in svc

    def test_timer_section_contains_oncalendar(self):
        from axiom.cli.scheduling.systemd import SystemdTimerBackend
        out = SystemdTimerBackend().render(
            schedule_name="x",
            cron="0 12 * * *",
            command="echo",
        )
        tmr = out.split("=== timer ===")[1]
        assert "[Timer]" in tmr
        assert "OnCalendar=" in tmr
        assert "Persistent=true" in tmr
        # WantedBy=timers.target so `enable` works
        assert "WantedBy=timers.target" in tmr

    def test_exact_cron_translates_to_calendar(self):
        from axiom.cli.scheduling.systemd import SystemdTimerBackend
        out = SystemdTimerBackend().render(
            schedule_name="x", cron="0 12 * * *", command="echo",
        )
        assert "OnCalendar=*-*-* 12:00:00" in out

    def test_step_minute_translates_to_calendar(self):
        from axiom.cli.scheduling.systemd import SystemdTimerBackend
        out = SystemdTimerBackend().render(
            schedule_name="x", cron="*/15 * * * *", command="echo",
        )
        assert "OnCalendar=*-*-* *:0/15:00" in out

    def test_step_hour_translates_to_calendar(self):
        from axiom.cli.scheduling.systemd import SystemdTimerBackend
        out = SystemdTimerBackend().render(
            schedule_name="x", cron="0 */6 * * *", command="echo",
        )
        assert "OnCalendar=*-*-* 0/6:00:00" in out

    def test_list_translates_to_comma_list(self):
        from axiom.cli.scheduling.systemd import SystemdTimerBackend
        out = SystemdTimerBackend().render(
            schedule_name="x", cron="0 9,12,15 * * *", command="echo",
        )
        assert "OnCalendar=*-*-* 9,12,15:00:00" in out

    def test_range_translates_to_double_dot(self):
        from axiom.cli.scheduling.systemd import SystemdTimerBackend
        out = SystemdTimerBackend().render(
            schedule_name="x", cron="0 9-17 * * *", command="echo",
        )
        assert "OnCalendar=*-*-* 9..17:00:00" in out

    def test_macro_daily(self):
        from axiom.cli.scheduling.systemd import SystemdTimerBackend
        out = SystemdTimerBackend().render(
            schedule_name="x", cron="@daily", command="echo",
        )
        # systemd accepts `daily` natively for OnCalendar
        assert "OnCalendar=daily" in out

    def test_macro_hourly(self):
        from axiom.cli.scheduling.systemd import SystemdTimerBackend
        out = SystemdTimerBackend().render(
            schedule_name="x", cron="@hourly", command="echo",
        )
        assert "OnCalendar=hourly" in out


# ---------------------------------------------------------------------------
# install_artifact — full host-side lifecycle
# ---------------------------------------------------------------------------


class TestInstallArtifact:
    def test_writes_service_and_timer_then_reload_then_enable_now(self):
        from axiom.cli.scheduling.systemd import SystemdTimerBackend
        runner = MockRunner(host="test-host")
        runner.replies = _probe_replies(user_dir="/home/test/.config/systemd/user")
        runner.replies.append(("", "", 0))  # daemon-reload
        runner.replies.append(("", "", 0))  # enable --now

        artifact = SystemdTimerBackend().render(
            schedule_name="heartbeat", cron="0 12 * * *", command="echo hi",
        )
        SystemdTimerBackend().install_artifact(
            runner=runner, schedule_name="heartbeat", artifact_content=artifact,
        )

        # Two writes: service + timer
        paths = [w.remote_path for w in runner.writes]
        assert "/home/test/.config/systemd/user/heartbeat.service" in paths
        assert "/home/test/.config/systemd/user/heartbeat.timer" in paths

        cmds = [str(r.command) for r in runner.runs]
        assert any("daemon-reload" in c for c in cmds)
        assert any("enable" in c and "--now" in c and "heartbeat.timer" in c for c in cmds)

    def test_honors_AXIOM_SYSTEMD_USER_DIR_override(self):
        from axiom.cli.scheduling.systemd import SystemdTimerBackend
        runner = MockRunner(host="test-host")
        runner.replies = _probe_replies(user_dir="/tmp/systemd-user")
        runner.replies.append(("", "", 0))  # daemon-reload
        runner.replies.append(("", "", 0))  # enable

        artifact = SystemdTimerBackend().render(
            schedule_name="x", cron="@daily", command="echo",
        )
        SystemdTimerBackend().install_artifact(
            runner=runner, schedule_name="x", artifact_content=artifact,
        )
        paths = [w.remote_path for w in runner.writes]
        assert all(p.startswith("/tmp/systemd-user/") for p in paths)

    def test_enable_failure_raises(self):
        from axiom.cli.scheduling.systemd import SystemdTimerBackend
        runner = MockRunner(host="test-host")
        runner.replies = _probe_replies()
        runner.replies.append(("", "", 0))  # daemon-reload
        runner.replies.append(("", "Failed to enable unit\n", 1))  # enable failure

        artifact = SystemdTimerBackend().render(
            schedule_name="x", cron="@daily", command="echo",
        )
        with pytest.raises(RuntimeError, match="enable"):
            SystemdTimerBackend().install_artifact(
                runner=runner, schedule_name="x", artifact_content=artifact,
            )

    def test_install_artifact_with_malformed_artifact_raises(self):
        """If someone hands us a non-systemd artifact (missing both
        sections), error rather than installing a broken unit."""
        from axiom.cli.scheduling.systemd import SystemdTimerBackend
        runner = MockRunner(host="test-host")
        runner.replies = _probe_replies()

        with pytest.raises(ValueError, match="section"):
            SystemdTimerBackend().install_artifact(
                runner=runner, schedule_name="x", artifact_content="garbage",
            )


# ---------------------------------------------------------------------------
# uninstall
# ---------------------------------------------------------------------------


class TestUninstall:
    def test_disable_now_then_rm_then_daemon_reload(self):
        from axiom.cli.scheduling.systemd import SystemdTimerBackend
        runner = MockRunner(host="test-host")
        runner.replies = _probe_replies()
        runner.replies.append(("", "", 0))  # disable --now
        runner.replies.append(("", "", 0))  # rm
        runner.replies.append(("", "", 0))  # daemon-reload

        SystemdTimerBackend().uninstall(
            runner=runner, schedule_name="heartbeat",
        )

        cmds = [str(r.command) for r in runner.runs]
        assert any("disable" in c and "--now" in c and "heartbeat.timer" in c for c in cmds)
        assert any("rm" in c for c in cmds)
        assert any("daemon-reload" in c for c in cmds)

    def test_disable_failure_when_not_loaded_tolerated(self):
        from axiom.cli.scheduling.systemd import SystemdTimerBackend
        runner = MockRunner(host="test-host")
        runner.replies = _probe_replies()
        runner.replies.append(("", "Failed to disable unit: Unit file ... does not exist\n", 1))
        runner.replies.append(("", "", 0))  # rm
        runner.replies.append(("", "", 0))  # daemon-reload

        # Should NOT raise — uninstall on a missing schedule is a no-op
        SystemdTimerBackend().uninstall(
            runner=runner, schedule_name="not-there",
        )
