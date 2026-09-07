# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Protocol surface for `axi schedule install` (issue #205).

Three load-bearing types every Backend + Runner impl conforms to:

  `CompletedRun`      — uniform shape for "I just ran a command on the
                         host; here's what happened."
  `RemoteRunner`      — transport abstraction. SSH today; WinRM later.
                         The orchestrator depends on this Protocol, not
                         on `subprocess` or `ssh` directly.
  `SchedulerBackend`  — per-OS install lifecycle. CronBackend on Linux,
                         LaunchdBackend on macOS, SystemdTimerBackend
                         + WindowsTaskSchedulerBackend later.

Plus `InstallReport` / `InstallOutcome` — the orchestrator's return
shape for partial-failure reporting across many schedules on one host.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, Protocol, runtime_checkable

InstallStatus = Literal["installed", "unchanged", "failed", "skipped"]
_VALID_STATUSES = ("installed", "unchanged", "failed", "skipped")


@dataclass(frozen=True)
class CompletedRun:
    """Result of a single `RemoteRunner.run()` call."""

    stdout: str
    stderr: str
    returncode: int

    @property
    def ok(self) -> bool:
        return self.returncode == 0


@runtime_checkable
class RemoteRunner(Protocol):
    """Transport abstraction. Conformers: `SSHRunner` today,
    `WinRMRunner` later, `MockRunner` in tests.

    Narrow on purpose — just enough to install / uninstall a
    schedule artifact. Path-style operations (existence checks,
    permission tweaks) come through `run()` with the appropriate
    host-native command.
    """

    host: str

    def run(self, command: list[str] | str, *, input: str | None = None) -> CompletedRun:
        ...

    def write_file(self, remote_path: str, content: str) -> None:
        ...


@runtime_checkable
class SchedulerBackend(Protocol):
    """Per-OS install lifecycle. The orchestrator picks a backend by
    host's OS (or by explicit `--backend` flag); the backend handles
    artifact format, install verb, and idempotence model for that OS.
    """

    name: str

    def artifact_filename(self, schedule_name: str) -> str:
        """E.g. `<name>.cron`, `<name>.plist`, `<name>.timer`."""

    def render(self, *, schedule_name: str, cron: str, command: str) -> str:
        """Produce the artifact content (the thing that lives in
        `deploy/<host>/<name>.<ext>`)."""

    def install(
        self,
        *,
        runner: RemoteRunner,
        schedule_name: str,
        cron: str,
        command: str,
    ) -> None:
        """Apply the schedule on the remote host. Idempotent: re-install
        with the same `(schedule_name, host)` replaces the existing
        managed entry rather than appending."""

    def uninstall(
        self,
        *,
        runner: RemoteRunner,
        schedule_name: str,
    ) -> None:
        """Remove the named schedule from the remote host. No-op when
        the schedule isn't installed (don't error)."""


@dataclass(frozen=True)
class InstallOutcome:
    """One schedule's outcome from `install_schedules`."""

    schedule_name: str
    status: InstallStatus
    error: str = ""

    def __post_init__(self) -> None:
        if self.status not in _VALID_STATUSES:
            raise ValueError(
                f"status must be one of {_VALID_STATUSES}, got {self.status!r}"
            )


@dataclass(frozen=True)
class InstallReport:
    """Aggregate result of `install_schedules` against one host."""

    host: str
    backend: str
    outcomes: list[InstallOutcome] = field(default_factory=list)

    @property
    def all_ok(self) -> bool:
        return all(o.status != "failed" for o in self.outcomes)
