# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Local-macOS integration smoke for LaunchdBackend (issue #205, slice 9).

End-to-end loop:

  1. Render a plist for a far-future / never-fires schedule.
  2. install_artifact via LocalRunner with AXIOM_LAUNCH_AGENTS_DIR
     overridden to a tmp dir (so we never touch ~/Library/LaunchAgents/).
  3. Verify plist file exists in tmp dir.
  4. `launchctl list <label>` returns 0 (job loaded into gui/$UID).
  5. Re-install → still loaded, idempotent.
  6. uninstall → plist gone + `launchctl list <label>` returns non-zero.
  7. Always bootout + rm in teardown so a failed assertion can't leave
     a managed job on the user's launchd.

Skipped unless `RUN_LAUNCHD_TESTS=1` AND `sys.platform == "darwin"`.
Run via:

    RUN_LAUNCHD_TESTS=1 pytest -m integration \
        tests/cli/scheduling/test_launchd_integration.py -v

The schedule uses `0 0 29 2 *` (00:00 on Feb 29) — only fires in leap
years and won't fire during this test session even if cleanup glitches.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

SAFE_NEVER_CRON = "0 0 29 2 *"  # 00:00 Feb 29 — every 4 years, never during a CI run
SMOKE_SCHEDULE_NAME = "axi-schedule-launchd-smoke"
SMOKE_COMMAND = "/bin/echo axi-launchd-smoke"


def _launchd_tests_enabled() -> bool:
    return os.environ.get("RUN_LAUNCHD_TESTS") == "1"


def _is_macos() -> bool:
    return sys.platform == "darwin"


def _label() -> str:
    from axiom.cli.scheduling.launchd import LABEL_PREFIX
    return f"{LABEL_PREFIX}{SMOKE_SCHEDULE_NAME}"


def _uid() -> str:
    return str(os.getuid())


def _launchctl_list_returncode() -> int:
    """`launchctl list <label>` returns 0 when loaded, non-zero otherwise."""
    result = subprocess.run(
        ["launchctl", "list", _label()],
        capture_output=True, text=True, timeout=5,
    )
    return result.returncode


def _force_cleanup(agents_dir: Path) -> None:
    """Best-effort teardown — bootout the job and remove the plist.
    Always called in fixture teardown so failed assertions can't leave
    state behind."""
    subprocess.run(
        ["launchctl", "bootout", f"gui/{_uid()}/{_label()}"],
        capture_output=True, text=True, timeout=5,
    )
    plist = agents_dir / f"{_label()}.plist"
    plist.unlink(missing_ok=True)


@pytest.fixture
def agents_dir(tmp_path: Path, monkeypatch) -> Path:
    """Redirect LaunchAgents to a tmp dir for the duration of the test."""
    target = tmp_path / "LaunchAgents"
    target.mkdir()
    monkeypatch.setenv("AXIOM_LAUNCH_AGENTS_DIR", str(target))
    return target


@pytest.mark.integration
class TestLaunchdLocalSmoke:
    """Skipped unless RUN_LAUNCHD_TESTS=1 and running on macOS."""

    @pytest.fixture(autouse=True)
    def _gate(self, agents_dir):
        if not _launchd_tests_enabled():
            pytest.skip("RUN_LAUNCHD_TESTS not set")
        if not _is_macos():
            pytest.skip("LaunchdBackend smoke is macOS-only")
        yield
        _force_cleanup(agents_dir)

    def test_install_then_verify_then_uninstall(self, agents_dir: Path):
        from axiom.cli.scheduling import LaunchdBackend, LocalRunner

        runner = LocalRunner(host="localhost")
        backend = LaunchdBackend()

        artifact = backend.render(
            schedule_name=SMOKE_SCHEDULE_NAME,
            cron=SAFE_NEVER_CRON,
            command=SMOKE_COMMAND,
        )

        # 1. install
        backend.install_artifact(
            runner=runner,
            schedule_name=SMOKE_SCHEDULE_NAME,
            artifact_content=artifact,
        )
        plist_path = agents_dir / f"{_label()}.plist"
        assert plist_path.exists(), f"plist not written: {plist_path}"
        assert _launchctl_list_returncode() == 0, (
            f"launchctl list {_label()!r} returned non-zero — bootstrap "
            f"didn't take"
        )

        # 2. idempotence — re-install must succeed and leave the job loaded
        backend.install_artifact(
            runner=runner,
            schedule_name=SMOKE_SCHEDULE_NAME,
            artifact_content=artifact,
        )
        assert plist_path.exists()
        assert _launchctl_list_returncode() == 0

        # 3. uninstall
        backend.uninstall(runner=runner, schedule_name=SMOKE_SCHEDULE_NAME)
        assert not plist_path.exists(), (
            f"plist lingered after uninstall: {plist_path}"
        )
        assert _launchctl_list_returncode() != 0, (
            f"launchctl list {_label()!r} still returns 0 — bootout didn't take"
        )
