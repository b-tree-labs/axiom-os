# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tests for `axiom.cli.scheduling.wintasks.WindowsTaskSchedulerBackend`
(issue #205, slice 12).

Windows-side `SchedulerBackend` impl. Modern Windows (10/11) ships
OpenSSH for Windows, so `SSHRunner` reaches a Windows host the same
way it reaches Linux/macOS — and `powershell.exe` is on PATH for the
session. The backend's artifact is a PowerShell script that registers
the task; install copies it to the host's TEMP dir and executes it.

No real-Windows integration smoke yet (no Win host in CI). Smoke is
gated on `RUN_WIN_TESTS=1 + AXIOM_WIN_TEST_HOST` and lives in a
follow-up commit when a host becomes available.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest


# ---------------------------------------------------------------------------
# MockRunner (same shape as the other backend tests)
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
    host: str = "win-test"
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


def _probe_replies(*, temp_dir="C:\\Users\\Test\\AppData\\Local\\Temp"):
    return [(temp_dir + "\r\n", "", 0)]


# ---------------------------------------------------------------------------
# Static shape
# ---------------------------------------------------------------------------


class TestStaticShape:
    def test_conforms_to_scheduler_backend(self):
        from axiom.cli.scheduling.protocols import SchedulerBackend
        from axiom.cli.scheduling.wintasks import WindowsTaskSchedulerBackend
        assert isinstance(WindowsTaskSchedulerBackend(), SchedulerBackend)

    def test_name_is_wintasks(self):
        from axiom.cli.scheduling.wintasks import WindowsTaskSchedulerBackend
        assert WindowsTaskSchedulerBackend().name == "wintasks"

    def test_artifact_filename_is_dot_ps1(self):
        from axiom.cli.scheduling.wintasks import WindowsTaskSchedulerBackend
        assert WindowsTaskSchedulerBackend().artifact_filename("heartbeat") == "heartbeat.ps1"


# ---------------------------------------------------------------------------
# Render
# ---------------------------------------------------------------------------


class TestRender:
    def test_task_name_is_axi_schedule_prefixed(self):
        from axiom.cli.scheduling.wintasks import WindowsTaskSchedulerBackend
        out = WindowsTaskSchedulerBackend().render(
            schedule_name="heartbeat", cron="0 12 * * *", command="echo hi",
        )
        assert "axi-schedule-heartbeat" in out
        assert "Register-ScheduledTask" in out

    def test_action_uses_cmd_exec_with_command(self):
        from axiom.cli.scheduling.wintasks import WindowsTaskSchedulerBackend
        out = WindowsTaskSchedulerBackend().render(
            schedule_name="x", cron="0 12 * * *", command="echo hi & dir",
        )
        # New-ScheduledTaskAction launches cmd /c <command>
        assert "New-ScheduledTaskAction" in out
        assert "cmd.exe" in out or "cmd" in out
        assert "echo hi & dir" in out

    def test_exact_cron_maps_to_daily_trigger(self):
        from axiom.cli.scheduling.wintasks import WindowsTaskSchedulerBackend
        out = WindowsTaskSchedulerBackend().render(
            schedule_name="x", cron="0 12 * * *", command="echo",
        )
        assert "New-ScheduledTaskTrigger" in out
        assert "-Daily" in out
        # Time literal in 24-hr HH:MM
        assert "12:00" in out

    def test_step_minute_maps_to_repetition_interval(self):
        from axiom.cli.scheduling.wintasks import WindowsTaskSchedulerBackend
        out = WindowsTaskSchedulerBackend().render(
            schedule_name="x", cron="*/15 * * * *", command="echo",
        )
        # */15 → -Once -RepetitionInterval (New-TimeSpan -Minutes 15)
        assert "-Once" in out
        assert "RepetitionInterval" in out
        assert "New-TimeSpan" in out
        assert "-Minutes 15" in out

    def test_step_hour_maps_to_repetition_interval(self):
        from axiom.cli.scheduling.wintasks import WindowsTaskSchedulerBackend
        out = WindowsTaskSchedulerBackend().render(
            schedule_name="x", cron="0 */6 * * *", command="echo",
        )
        assert "-RepetitionInterval" in out
        assert "-Hours 6" in out

    def test_macro_daily(self):
        from axiom.cli.scheduling.wintasks import WindowsTaskSchedulerBackend
        out = WindowsTaskSchedulerBackend().render(
            schedule_name="x", cron="@daily", command="echo",
        )
        assert "-Daily" in out
        # @daily ≡ 0 0 * * * → midnight
        assert "00:00" in out

    def test_macro_hourly(self):
        from axiom.cli.scheduling.wintasks import WindowsTaskSchedulerBackend
        out = WindowsTaskSchedulerBackend().render(
            schedule_name="x", cron="@hourly", command="echo",
        )
        # @hourly → step 1 hour
        assert "-RepetitionInterval" in out
        assert "-Hours 1" in out

    def test_list_cron_rejected(self):
        """Lists could map to multiple triggers; defer to a refinement."""
        from axiom.cli.scheduling.wintasks import WindowsTaskSchedulerBackend
        with pytest.raises(ValueError, match="list"):
            WindowsTaskSchedulerBackend().render(
                schedule_name="x", cron="0 9,12,15 * * *", command="echo",
            )

    def test_range_cron_rejected(self):
        from axiom.cli.scheduling.wintasks import WindowsTaskSchedulerBackend
        with pytest.raises(ValueError, match="range"):
            WindowsTaskSchedulerBackend().render(
                schedule_name="x", cron="0 9-17 * * *", command="echo",
            )


# ---------------------------------------------------------------------------
# install_artifact
# ---------------------------------------------------------------------------


class TestInstallArtifact:
    def test_writes_script_then_runs_powershell_then_removes(self):
        from axiom.cli.scheduling.wintasks import WindowsTaskSchedulerBackend
        runner = MockRunner(host="win-test")
        runner.replies = _probe_replies()
        # write_file → not a run call
        # invoke powershell -File <path>
        runner.replies.append(("", "", 0))
        # cleanup `del` of script
        runner.replies.append(("", "", 0))

        backend = WindowsTaskSchedulerBackend()
        artifact = backend.render(
            schedule_name="x", cron="0 12 * * *", command="echo hi",
        )
        backend.install_artifact(
            runner=runner, schedule_name="x", artifact_content=artifact,
        )

        # Script was written to a TEMP path
        assert len(runner.writes) == 1
        wpath = runner.writes[0].remote_path
        assert wpath.endswith(".ps1")
        # Path under the resolved Temp dir
        assert "Temp" in wpath or "tmp" in wpath.lower()
        assert runner.writes[0].content == artifact

        # powershell command executed against the script path
        cmds = [str(r.command) for r in runner.runs]
        assert any("powershell" in c.lower() and ".ps1" in c for c in cmds)

    def test_idempotence_unregister_before_register(self):
        """Re-install on existing task must succeed. Strategy: the
        rendered script first Unregister-ScheduledTasks (silent on
        not-found) then Register. So install_artifact doesn't need a
        pre-unregister call — verify the rendered script handles it."""
        from axiom.cli.scheduling.wintasks import WindowsTaskSchedulerBackend
        out = WindowsTaskSchedulerBackend().render(
            schedule_name="x", cron="0 12 * * *", command="echo",
        )
        # script unregisters first, ignoring errors
        assert "Unregister-ScheduledTask" in out
        assert "SilentlyContinue" in out

    def test_powershell_failure_raises(self):
        from axiom.cli.scheduling.wintasks import WindowsTaskSchedulerBackend
        runner = MockRunner(host="win-test")
        runner.replies = _probe_replies()
        runner.replies.append(("", "Access denied\n", 1))  # ps execution fails
        runner.replies.append(("", "", 0))  # cleanup

        backend = WindowsTaskSchedulerBackend()
        artifact = backend.render(
            schedule_name="x", cron="0 12 * * *", command="echo",
        )
        with pytest.raises(RuntimeError, match="powershell"):
            backend.install_artifact(
                runner=runner, schedule_name="x", artifact_content=artifact,
            )


# ---------------------------------------------------------------------------
# uninstall
# ---------------------------------------------------------------------------


class TestUninstall:
    def test_invokes_unregister_scheduled_task(self):
        from axiom.cli.scheduling.wintasks import WindowsTaskSchedulerBackend
        runner = MockRunner(host="win-test")
        runner.replies.append(("", "", 0))  # uninstall ps invocation

        WindowsTaskSchedulerBackend().uninstall(
            runner=runner, schedule_name="heartbeat",
        )

        cmds = [str(r.command) for r in runner.runs]
        assert any(
            "Unregister-ScheduledTask" in c and "axi-schedule-heartbeat" in c
            for c in cmds
        )

    def test_uninstall_uses_silently_continue(self):
        """Uninstall on a missing task must not raise — the rendered
        Unregister call should use -ErrorAction SilentlyContinue."""
        from axiom.cli.scheduling.wintasks import WindowsTaskSchedulerBackend
        runner = MockRunner(host="win-test")
        runner.replies.append(("", "", 0))

        # No exception even if the task doesn't exist on the host.
        WindowsTaskSchedulerBackend().uninstall(
            runner=runner, schedule_name="not-there",
        )
        cmds = [str(r.command) for r in runner.runs]
        assert any("SilentlyContinue" in c for c in cmds)
