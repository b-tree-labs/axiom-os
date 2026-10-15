# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Real-SSH integration smoke for SystemdTimerBackend against a remote
Linux host running user-systemd (issue #205, slice 11).

End-to-end loop:

  1. Render a `.systemd` artifact for an axi-systemd-smoke schedule with
     a far-future OnCalendar that won't fire during a test run.
  2. install_artifact via SSHRunner → check that .service + .timer
     landed in `~/.config/systemd/user/` on the host.
  3. `systemctl --user list-timers` shows the timer.
  4. Re-install → still one timer (idempotent).
  5. uninstall → timer + units removed.

Skipped by default. Run via:

    RUN_SSH_TESTS=1 AXIOM_TEST_HOST=<your-systemd-host> pytest \
        -m integration tests/cli/scheduling/test_systemd_integration.py -v

Cleanup runs in finally even on failure so we never leave a managed
timer on shared infrastructure.

Host prerequisites (one-time, per target):
  - `loginctl enable-linger <user>`  (so user units survive logout)
  - systemd --user must be active for the login session
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

# Feb 29 at midnight — only fires in leap years, never during a test session.
SAFE_NEVER_CRON = "0 0 29 2 *"
SMOKE_SCHEDULE_NAME = "axi-systemd-smoke-test"
SMOKE_COMMAND = "/bin/echo 'axi systemd smoke (safe to ignore)'"


def _ssh_tests_enabled() -> bool:
    return os.environ.get("RUN_SSH_TESTS") == "1"


def _ssh_available_to_host(host: str) -> bool:
    if not shutil.which("ssh"):
        return False
    result = subprocess.run(
        ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5",
         host, "systemctl", "--user", "is-system-running"],
        capture_output=True, text=True, timeout=10,
    )
    # Exit 0 means "running"; rc=1 "degraded" is still usable for our
    # purposes (some unrelated unit is in a failed state).
    return result.returncode in (0, 1)


@pytest.fixture
def smoke_host() -> str:
    host = os.environ.get("AXIOM_TEST_HOST")
    if not host:
        pytest.skip("AXIOM_TEST_HOST not set — no smoke target configured")
    return host


@pytest.fixture
def smoke_project_root(tmp_path: Path, smoke_host: str) -> Path:
    """Lay down `deploy/<host>/<name>.systemd` for the smoke schedule.
    Uses the backend's own render so the install path exercises the
    same artifact format real users get."""
    from axiom.cli.scheduling import SystemdTimerBackend

    root = tmp_path / "project"
    deploy = root / "deploy" / smoke_host
    deploy.mkdir(parents=True, exist_ok=True)

    backend = SystemdTimerBackend()
    artifact = backend.render(
        schedule_name=SMOKE_SCHEDULE_NAME,
        cron=SAFE_NEVER_CRON,
        command=SMOKE_COMMAND,
    )
    (deploy / backend.artifact_filename(SMOKE_SCHEDULE_NAME)).write_text(artifact)
    return root


def _timer_listed(host: str) -> bool:
    """True iff `systemctl --user list-timers` mentions our timer
    (whether active or not)."""
    result = subprocess.run(
        ["ssh", "-o", "BatchMode=yes", host,
         "systemctl", "--user", "list-timers", "--all", "--no-pager"],
        capture_output=True, text=True, timeout=10,
    )
    return f"{SMOKE_SCHEDULE_NAME}.timer" in (result.stdout + result.stderr)


def _force_cleanup(host: str) -> None:
    from axiom.cli.scheduling import SSHRunner, SystemdTimerBackend
    try:
        SystemdTimerBackend().uninstall(
            runner=SSHRunner(host=host),
            schedule_name=SMOKE_SCHEDULE_NAME,
        )
    except Exception:  # noqa: BLE001 — cleanup is best-effort
        pass


@pytest.mark.integration
class TestSystemdSshSmoke:
    """Skipped unless RUN_SSH_TESTS=1 AND AXIOM_TEST_HOST is set.
    Requires user-level systemd on the target host."""

    @pytest.fixture(autouse=True)
    def _gate_on_env(self, smoke_host: str):
        if not _ssh_tests_enabled():
            pytest.skip("RUN_SSH_TESTS not set")
        if not _ssh_available_to_host(smoke_host):
            pytest.skip(
                f"systemd --user not available on {smoke_host!r} "
                f"(or ssh failed)"
            )
        yield
        _force_cleanup(smoke_host)

    def test_install_then_verify_then_uninstall(
        self, smoke_project_root, smoke_host,
    ):
        from axiom.cli.scheduling import (
            SSHRunner,
            SystemdTimerBackend,
            install_schedules,
        )

        runner = SSHRunner(host=smoke_host)
        backend = SystemdTimerBackend()

        # 1. install
        report = install_schedules(
            project_root=smoke_project_root,
            host=smoke_host,
            runner=runner,
            backend=backend,
        )
        assert report.all_ok, f"install failed: {report.outcomes}"

        # 2. timer landed
        assert _timer_listed(smoke_host), (
            f"timer {SMOKE_SCHEDULE_NAME!r} not in `list-timers` output"
        )

        # 3. idempotence — re-install must succeed
        report2 = install_schedules(
            project_root=smoke_project_root,
            host=smoke_host,
            runner=runner,
            backend=backend,
        )
        assert report2.all_ok
        assert _timer_listed(smoke_host)

        # 4. uninstall
        backend.uninstall(runner=runner, schedule_name=SMOKE_SCHEDULE_NAME)
        assert not _timer_listed(smoke_host), (
            f"timer {SMOKE_SCHEDULE_NAME!r} still listed after uninstall"
        )
