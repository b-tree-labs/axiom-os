# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Real-SSH integration smoke for `axi schedule install` against a
remote Linux/Unix host running traditional cron (issue #205, slice 6).

End-to-end loop:

  1. Render a test schedule artifact under `<tmp>/deploy/<host>/`.
  2. install_schedules → installed status.
  3. `ssh <host> crontab -l` → marker line present.
  4. install_schedules again → still one marker (idempotence).
  5. backend.uninstall → marker line gone.
  6. Always re-uninstall in finally (no leftover crontab entries on
     the test host even when assertions fail mid-run).

Skipped by default. Run via:

    RUN_SSH_TESTS=1 AXIOM_TEST_HOST=<your-cron-host> pytest \
        -m integration tests/cli/scheduling/test_cron_ssh_integration.py -v

`AXIOM_TEST_HOST` must be ssh-reachable with key-based auth and have a
working crontab. The smoke uses a far-future cron expression (Feb 29 +
weekday Monday + 23:59) so even if cleanup fails it can NEVER actually
fire on the host — defensive choice given the smoke runs against
shared infrastructure.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

# Far-future / never-actually-fires cron expression.
# `59 23 29 2 1` = 23:59 on Feb 29th when weekday is Monday.
# Feb 29 only happens in leap years; combined with weekday=Monday it's
# extremely rare. Cleanup will still remove the marker either way.
SAFE_NEVER_CRON = "59 23 29 2 1"

SMOKE_SCHEDULE_NAME = "axi-schedule-smoke-test"
SMOKE_COMMAND = "echo 'axi schedule smoke test (safe to ignore)'"


def _ssh_tests_enabled() -> bool:
    return os.environ.get("RUN_SSH_TESTS") == "1"


def _ssh_available_to_host(host: str) -> bool:
    """Probe ssh connectivity before running the actual smoke. Without
    this, a missing/incorrect host yields a confusing test failure
    instead of a clean skip."""
    if not shutil.which("ssh"):
        return False
    result = subprocess.run(
        ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5",
         host, "true"],
        capture_output=True,
        text=True,
        timeout=10,
    )
    return result.returncode == 0


@pytest.fixture
def smoke_host() -> str:
    host = os.environ.get("AXIOM_TEST_HOST")
    if not host:
        pytest.skip("AXIOM_TEST_HOST not set — no smoke target configured")
    return host


@pytest.fixture
def smoke_project_root(tmp_path: Path, smoke_host: str) -> Path:
    """Lay down `deploy/<host>/<name>.cron` for the smoke schedule.
    Uses the CronBackend's render output so the install path exercises
    the same artifact format real users get."""
    from axiom.cli.scheduling import CronBackend

    root = tmp_path / "project"
    deploy = root / "deploy" / smoke_host
    deploy.mkdir(parents=True, exist_ok=True)

    backend = CronBackend()
    artifact = backend.render(
        schedule_name=SMOKE_SCHEDULE_NAME,
        cron=SAFE_NEVER_CRON,
        command=SMOKE_COMMAND,
    )
    (deploy / backend.artifact_filename(SMOKE_SCHEDULE_NAME)).write_text(artifact)
    return root


def _crontab_listing(host: str) -> str:
    """Read `crontab -l` on `host`. Empty when no crontab."""
    result = subprocess.run(
        ["ssh", "-o", "BatchMode=yes", host, "crontab", "-l"],
        capture_output=True,
        text=True,
        timeout=10,
    )
    # cron returns rc=1 + "no crontab for" on stderr when none exists.
    if result.returncode != 0 and "no crontab" in result.stderr.lower():
        return ""
    return result.stdout


def _force_cleanup(host: str) -> None:
    """Best-effort removal of the smoke marker from the host's crontab.
    Always called in finally — leftover entries on shared
    infrastructure are worse than a flaky test."""
    from axiom.cli.scheduling import CronBackend, SSHRunner
    try:
        CronBackend().uninstall(
            runner=SSHRunner(host=host),
            schedule_name=SMOKE_SCHEDULE_NAME,
        )
    except Exception:  # noqa: BLE001 — cleanup is best-effort
        pass


@pytest.mark.integration
class TestCronSshSmoke:
    """Skipped unless RUN_SSH_TESTS=1 AND AXIOM_TEST_HOST is set.
    Requires ssh-key auth to the smoke host."""

    @pytest.fixture(autouse=True)
    def _gate_on_env(self, smoke_host: str):
        if not _ssh_tests_enabled():
            pytest.skip("RUN_SSH_TESTS not set")
        if not _ssh_available_to_host(smoke_host):
            pytest.skip(f"ssh to {smoke_host!r} not available (BatchMode probe failed)")
        yield
        # autouse teardown — clean up no matter what.
        _force_cleanup(smoke_host)

    def test_install_then_verify_then_uninstall(
        self, smoke_project_root, smoke_host,
    ):
        from axiom.cli.scheduling import (
            CronBackend,
            SSHRunner,
            install_schedules,
        )

        runner = SSHRunner(host=smoke_host)
        backend = CronBackend()

        # 1. install
        report = install_schedules(
            project_root=smoke_project_root,
            host=smoke_host,
            runner=runner,
            backend=backend,
        )
        assert report.all_ok, f"install failed: {report.outcomes}"
        assert any(
            o.schedule_name == SMOKE_SCHEDULE_NAME and o.status == "installed"
            for o in report.outcomes
        )

        # 2. crontab verification
        listing = _crontab_listing(smoke_host)
        marker = f"# axi schedule managed: {SMOKE_SCHEDULE_NAME}"
        assert marker in listing, (
            f"smoke marker not found in remote crontab:\n{listing}"
        )
        assert SAFE_NEVER_CRON in listing
        assert listing.count(marker) == 1

        # 3. idempotence — re-install
        report2 = install_schedules(
            project_root=smoke_project_root,
            host=smoke_host,
            runner=runner,
            backend=backend,
        )
        assert report2.all_ok
        listing2 = _crontab_listing(smoke_host)
        assert listing2.count(marker) == 1, (
            f"re-install duplicated the line:\n{listing2}"
        )

        # 4. uninstall + verification
        backend.uninstall(runner=runner, schedule_name=SMOKE_SCHEDULE_NAME)
        listing3 = _crontab_listing(smoke_host)
        assert marker not in listing3, (
            f"marker lingered after uninstall:\n{listing3}"
        )
