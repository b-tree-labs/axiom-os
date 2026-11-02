# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Backend auto-detection (issue #205, slice 13).

`axi schedule install --host X` without `--backend` (or `--backend auto`)
probes the host via the runner and picks the right SchedulerBackend:

  Darwin                                → launchd
  Linux + systemctl available           → systemd
  Linux without systemctl               → cron
  Windows (uname missing or MINGW/MSYS) → wintasks
  Otherwise                             → cron (POSIX fallback)

Probes are read-only: `uname -s`, `command -v systemctl`, `ver`. Each
command runs through the same `RemoteRunner` instance the install will
use, so detection works identically over SSH or local.
"""

from __future__ import annotations

from .protocols import RemoteRunner


def detect_backend(runner: RemoteRunner) -> str:
    """Return the name of the backend best suited for `runner.host`.

    Always returns a valid `--backend` choice — falls back to ``cron``
    if no probe succeeds (rather than raising) so the install still
    has a chance to proceed.
    """
    uname_result = runner.run(["uname", "-s"])
    if uname_result.ok and uname_result.stdout.strip():
        kernel = uname_result.stdout.strip()
        if "Darwin" in kernel:
            return "launchd"
        if "Linux" in kernel:
            return _linux_choice(runner)
        if "MINGW" in kernel or "MSYS" in kernel or "CYGWIN" in kernel:
            return "wintasks"
        # Unknown POSIX kernel (HP-UX, AIX, SunOS, ...) — cron is the
        # universal POSIX denominator. Don't try to be cleverer than
        # the user's host.
        return "cron"

    # uname didn't run — likely Windows with cmd.exe as the ssh shell.
    ver_result = runner.run("ver")
    if ver_result.ok and "Windows" in ver_result.stdout:
        return "wintasks"

    return "cron"


def _linux_choice(runner: RemoteRunner) -> str:
    """systemd-or-cron decision on Linux. `command -v systemctl` is a
    POSIX-spec built-in; exit 0 iff systemctl is on PATH."""
    probe = runner.run(["command", "-v", "systemctl"])
    if probe.ok and probe.stdout.strip():
        return "systemd"
    return "cron"
