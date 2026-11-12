# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""`WindowsTaskSchedulerBackend` — Windows scheduler impl (issue #205, slice 12).

Targets Windows 10 / 11 hosts reachable via OpenSSH for Windows
(`ssh user@winhost`). On the wire the same as Linux/macOS, but the
remote shell is PowerShell — so the artifact is a `.ps1` script that
registers a scheduled task with `Register-ScheduledTask`.

Idempotence model: the rendered script begins with an Unregister
(SilentlyContinue) so re-install replaces the existing task. install
copies the script to the host's TEMP dir, runs it with
`powershell -ExecutionPolicy Bypass -File`, then removes the script.

No integration smoke yet — no Windows host available. Smoke is gated
on `RUN_WIN_TESTS=1 + AXIOM_WIN_TEST_HOST` and ships in a follow-up
when a target host is in place.
"""

from __future__ import annotations

from datetime import UTC, datetime

from .protocols import RemoteRunner

TASK_NAME_PREFIX = "axi-schedule-"


# ---------------------------------------------------------------------------
# Cron → Windows Task Scheduler trigger
# ---------------------------------------------------------------------------


def _cron_to_trigger_block(cron: str) -> str:
    """Translate a cron expression to a PowerShell `$trigger = ...` block.

    Supports:
      `0 12 * * *`       → -Daily -At "12:00"
      `*/N * * * *`      → -Once -At (Get-Date) -RepetitionInterval (New-TimeSpan -Minutes N)
      `0 */N * * *`      → -Once -At (Get-Date) -RepetitionInterval (New-TimeSpan -Hours N)
      `@hourly`          → -Once + RepetitionInterval 1 hour
      `@daily`/@midnight → -Daily -At "00:00"

    Lists/ranges raise ValueError — multiple triggers could express
    them but adds complexity better deferred until a real user asks.
    """
    cron = cron.strip()
    macro_map = {
        "@hourly":   "0 */1 * * *",
        "@daily":    "0 0 * * *",
        "@midnight": "0 0 * * *",
    }
    if cron in macro_map:
        cron = macro_map[cron]
    elif cron.startswith("@"):
        raise ValueError(f"unsupported cron macro for wintasks: {cron!r}")

    fields = cron.split()
    if len(fields) != 5:
        raise ValueError(
            f"cron expression must have 5 fields, got {len(fields)}: {cron!r}"
        )
    minute, hour, day, month, weekday = fields

    for field, raw in (("minute", minute), ("hour", hour), ("day", day),
                       ("month", month), ("weekday", weekday)):
        if "," in raw:
            raise ValueError(
                f"cron list expressions aren't expressible in this initial "
                f"WinTask cut: {field}={raw!r}"
            )
        if "-" in raw:
            raise ValueError(
                f"cron range expressions aren't expressible in this initial "
                f"WinTask cut: {field}={raw!r}"
            )

    # Minute step
    if minute.startswith("*/") and hour == "*" and day == "*" and month == "*" and weekday == "*":
        try:
            step = int(minute[2:])
        except ValueError as exc:
            raise ValueError(f"invalid minute step: {minute!r}") from exc
        return (
            f"$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date) "
            f"-RepetitionInterval (New-TimeSpan -Minutes {step})"
        )

    # Hour step
    if minute == "0" and hour.startswith("*/") and day == "*" and month == "*" and weekday == "*":
        try:
            step = int(hour[2:])
        except ValueError as exc:
            raise ValueError(f"invalid hour step: {hour!r}") from exc
        return (
            f"$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date) "
            f"-RepetitionInterval (New-TimeSpan -Hours {step})"
        )

    # Exact daily
    if (not minute.startswith("*")) and (not hour.startswith("*")) \
            and day == "*" and month == "*" and weekday == "*":
        try:
            h = int(hour)
            m = int(minute)
        except ValueError as exc:
            raise ValueError(
                f"unparsable exact hour/minute: {hour!r}/{minute!r}"
            ) from exc
        return f'$trigger = New-ScheduledTaskTrigger -Daily -At "{h:02d}:{m:02d}"'

    raise ValueError(
        f"cron expression {cron!r} can't be translated to a Windows Task "
        f"Scheduler trigger in this initial cut — supported shapes: "
        f"exact daily (H M * * *), minute step (*/N * * * *), hour step "
        f"(0 */N * * *), and macros @hourly/@daily/@midnight"
    )


# ---------------------------------------------------------------------------
# PowerShell script emission
# ---------------------------------------------------------------------------


def _ps_escape_single(s: str) -> str:
    """Escape a string for PowerShell single-quoted literals: '' inside ''."""
    return s.replace("'", "''")


def _emit_script(*, task_name: str, trigger_block: str, command: str,
                 generated_at: str) -> str:
    safe_command = _ps_escape_single(command)
    return (
        f"# Generated by `axi schedule` on {generated_at}\n"
        f"# Task: {task_name}\n"
        "$ErrorActionPreference = 'Stop'\n"
        "\n"
        f"Unregister-ScheduledTask -TaskName '{task_name}' "
        "-Confirm:$false -ErrorAction SilentlyContinue\n"
        "\n"
        f"{trigger_block}\n"
        "$action = New-ScheduledTaskAction "
        "-Execute 'cmd.exe' "
        f"-Argument '/c {safe_command}'\n"
        "$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable\n"
        "\n"
        "Register-ScheduledTask "
        f"-TaskName '{task_name}' "
        "-Trigger $trigger "
        "-Action $action "
        "-Settings $settings "
        "| Out-Null\n"
    )


# ---------------------------------------------------------------------------
# Runner helpers
# ---------------------------------------------------------------------------


def _resolve_temp_dir(runner: RemoteRunner) -> str:
    """Resolve $env:TEMP on the Windows host. PowerShell-friendly form
    works whether the shell is PowerShell (the OpenSSH default) or
    Windows cmd (rare). Passed as a string so SSHRunner forwards it
    verbatim instead of re-flattening argv."""
    result = runner.run('powershell -NoProfile -Command "Write-Host $env:TEMP"')
    if not result.ok:
        raise RuntimeError(
            f"could not resolve TEMP on {runner.host!r}: "
            f"rc={result.returncode} stderr={result.stderr!r}"
        )
    return result.stdout.strip()


def _run_powershell_file(runner: RemoteRunner, ps_path: str) -> None:
    cmd = f'powershell -NoProfile -ExecutionPolicy Bypass -File "{ps_path}"'
    result = runner.run(cmd)
    if not result.ok:
        raise RuntimeError(
            f"powershell -File {ps_path!r} failed on {runner.host!r}: "
            f"rc={result.returncode} stderr={result.stderr!r}"
        )


def _run_powershell_command(runner: RemoteRunner, ps_command: str) -> None:
    cmd = f'powershell -NoProfile -Command "{ps_command}"'
    runner.run(cmd)


def _task_name_for(schedule_name: str) -> str:
    return f"{TASK_NAME_PREFIX}{schedule_name}"


# ---------------------------------------------------------------------------
# Backend
# ---------------------------------------------------------------------------


class WindowsTaskSchedulerBackend:
    """SchedulerBackend impl for Windows Task Scheduler via PowerShell."""

    name: str = "wintasks"

    def artifact_filename(self, schedule_name: str) -> str:
        return f"{schedule_name}.ps1"

    def render(self, *, schedule_name: str, cron: str, command: str) -> str:
        task_name = _task_name_for(schedule_name)
        trigger_block = _cron_to_trigger_block(cron)
        timestamp = datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC")
        return _emit_script(
            task_name=task_name,
            trigger_block=trigger_block,
            command=command,
            generated_at=timestamp,
        )

    def install_artifact(
        self,
        *,
        runner: RemoteRunner,
        schedule_name: str,
        artifact_content: str,
    ) -> None:
        temp_dir = _resolve_temp_dir(runner)
        ps_path = f"{temp_dir}\\axi-schedule-{schedule_name}.ps1"
        runner.write_file(ps_path, artifact_content)
        try:
            _run_powershell_file(runner, ps_path)
        finally:
            # Best-effort cleanup; ignore failure.
            runner.run(f'cmd /c del /q "{ps_path}"')

    def install(
        self,
        *,
        runner: RemoteRunner,
        schedule_name: str,
        cron: str,
        command: str,
    ) -> None:
        artifact = self.render(
            schedule_name=schedule_name, cron=cron, command=command,
        )
        self.install_artifact(
            runner=runner,
            schedule_name=schedule_name,
            artifact_content=artifact,
        )

    def uninstall(
        self,
        *,
        runner: RemoteRunner,
        schedule_name: str,
    ) -> None:
        task_name = _task_name_for(schedule_name)
        ps_command = (
            f"Unregister-ScheduledTask -TaskName '{task_name}' "
            "-Confirm:`$false -ErrorAction SilentlyContinue"
        )
        _run_powershell_command(runner, ps_command)
