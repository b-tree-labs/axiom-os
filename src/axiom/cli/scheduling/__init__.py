# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Schedule install primitives — apply side of `axi schedule` (issue #205).

Public surface:

  install_schedules    — orchestrator over Backend + Runner + artifacts
  SchedulerBackend     — per-OS install lifecycle Protocol
  RemoteRunner         — transport Protocol
  CronBackend          — Linux cron impl
  SSHRunner            — ssh-CLI-backed RemoteRunner impl
  InstallReport        — per-host aggregate outcome
  InstallOutcome       — per-schedule outcome
"""

from __future__ import annotations

from .cron import CronBackend
from .detect import detect_backend
from .install import install_schedules
from .launchd import LaunchdBackend
from .local import LocalRunner
from .protocols import (
    CompletedRun,
    InstallOutcome,
    InstallReport,
    InstallStatus,
    RemoteRunner,
    SchedulerBackend,
)
from .ssh import SSHRunner
from .systemd import SystemdTimerBackend
from .wintasks import WindowsTaskSchedulerBackend

__all__ = [
    "CompletedRun",
    "CronBackend",
    "InstallOutcome",
    "InstallReport",
    "InstallStatus",
    "LaunchdBackend",
    "LocalRunner",
    "RemoteRunner",
    "SchedulerBackend",
    "SSHRunner",
    "SystemdTimerBackend",
    "WindowsTaskSchedulerBackend",
    "detect_backend",
    "install_schedules",
]
