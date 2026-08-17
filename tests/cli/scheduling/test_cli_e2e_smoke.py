# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""End-to-end CLI smoke for `axi schedule` (issue #205, slice 16).

Unit tests call `main()` directly; backend smokes import LaunchdBackend
or SystemdTimerBackend and exercise the Python API. Neither catches:

  - Missing `if __name__ == "__main__"` guard (`python -m` silently exits)
  - `axi schedule create` writing artifacts that `axi schedule install`
    refuses to parse (marker / format drift between the two verbs)
  - Auto-detect picking a backend whose artifact extension doesn't
    match what `create` wrote → silent "no schedule artifacts" instead
    of an actionable error
  - Output text users actually see (the prior tests assert on report
    objects, not stdout strings)

This smoke runs the CLI as a subprocess against `python -m` (the user's
entry point) and asserts on stdout. Fast: only the always-on local
paths (no host touch). Gated SSH smokes against AXIOM_TEST_HOST live
in test_cron_ssh_integration.py and test_systemd_integration.py.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest


REPO_SRC = Path(__file__).resolve().parents[3] / "src"


def _run_axi(args: list[str], *, cwd: Path, axiom_root: Path) -> subprocess.CompletedProcess:
    """Invoke `python -m axiom.cli.schedule` as a subprocess.

    Bypasses any installed `axi` binary so the test exercises the
    code in this checkout, not whatever's pip-installed system-wide.
    """
    env = {**os.environ, "AXIOM_ROOT": str(axiom_root)}
    # Prepend our src/ to PYTHONPATH so axiom imports resolve here.
    existing = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = f"{REPO_SRC}{os.pathsep}{existing}" if existing else str(REPO_SRC)
    return subprocess.run(
        [sys.executable, "-m", "axiom.cli.schedule", *args],
        cwd=str(cwd),
        env=env,
        capture_output=True,
        text=True,
        timeout=15,
    )


@pytest.fixture
def project_root(tmp_path: Path) -> Path:
    root = tmp_path / "project"
    root.mkdir()
    (root / ".git").mkdir()
    return root


# ---------------------------------------------------------------------------
# `python -m` entry point exists and runs main()
# ---------------------------------------------------------------------------


class TestEntryPoint:
    def test_python_dash_m_runs_main_not_silent(self, project_root):
        """`python -m axiom.cli.schedule list` should print something —
        either rows or '(no schedule artifacts)'. A silent exit means
        the module-level `if __name__ == "__main__"` guard is missing."""
        result = _run_axi(["list"], cwd=project_root, axiom_root=project_root)
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip(), (
            "`python -m axiom.cli.schedule list` exited silently — "
            "likely missing the `if __name__ == '__main__'` guard"
        )


# ---------------------------------------------------------------------------
# `create` writes an artifact that `install` can actually parse
# ---------------------------------------------------------------------------


class TestCreateInstallContract:
    """The whole point of `axi schedule create` is that its output can
    be fed to `axi schedule install`. Test that contract explicitly —
    create → install (dry-run) → no parse failures."""

    def test_create_output_parseable_by_install_artifact(self, project_root):
        """Direct contract: what `create_schedule` writes must be what
        `CronBackend.install_artifact` can consume. They've drifted
        before (create wrote no marker; install_artifact required one
        → silent install failure)."""
        from axiom.cli.schedule import create_schedule
        from axiom.cli.scheduling import CronBackend
        from axiom.cli.scheduling.protocols import CompletedRun

        artifact_path = create_schedule(
            project_root=project_root, name="heartbeat", host="test-host",
            cron="*/15 * * * *", command="echo hi",
        )
        artifact = artifact_path.read_text()

        class _NoOpRunner:
            host = "test"
            def run(self, *args, **kwargs):
                return CompletedRun("", "", 0)
            def write_file(self, *args, **kwargs):
                pass

        # If create's output is missing the marker, this raises ValueError.
        CronBackend().install_artifact(
            runner=_NoOpRunner(),
            schedule_name="heartbeat",
            artifact_content=artifact,
        )

    def test_created_artifact_installs_cleanly_via_dry_run(self, project_root):
        # Create
        result = _run_axi(
            ["create", "heartbeat", "--host", "test-host",
             "--cron", "*/15 * * * *",
             "--command", "bash ${REPO_DIR}/scripts/hb.sh"],
            cwd=project_root, axiom_root=project_root,
        )
        assert result.returncode == 0, result.stderr
        assert "Wrote" in result.stdout

        # The artifact must exist
        artifacts = list((project_root / "deploy" / "test-host").glob("heartbeat.*"))
        assert artifacts, "no artifact written under deploy/test-host/"

        # Install --dry-run with explicit cron backend — must NOT report failure
        result = _run_axi(
            ["install", "--host", "test-host", "--backend", "cron", "--dry-run"],
            cwd=project_root, axiom_root=project_root,
        )
        assert result.returncode == 0, result.stderr
        # If create wrote a marker'd line, dry-run reports SKIPPED.
        # If create wrote an unmarker'd line, the actual install path
        # would fail to parse — surface that here too.
        assert "FAILED" not in result.stdout, (
            f"dry-run reported a parse failure on a freshly-created "
            f"artifact — create and install have drifted apart:\n"
            f"{result.stdout}"
        )
        assert "heartbeat" in result.stdout


# ---------------------------------------------------------------------------
# `install --host X` without `--backend` finds the artifacts that
# `create --host X` wrote (auto-detect path)
# ---------------------------------------------------------------------------


class TestCreateBackendMismatch:
    """The real bug: `axi schedule create` always writes `.cron`, but
    `axi schedule install` with auto-detect on a macOS/systemd host
    looks for `.plist`/`.systemd` and silently finds nothing.

    Until `create` becomes backend-aware (or we adopt a neutral
    descriptor format), this scenario must at least *fail loudly* with
    a message telling the user what to do."""

    def test_install_with_mismatched_backend_does_not_silently_report_empty(
        self, project_root,
    ):
        # Create writes a .cron file (today's default)
        _run_axi(
            ["create", "dev-poll", "--host", "test-host",
             "--cron", "*/5 * * * *", "--command", "echo hi"],
            cwd=project_root, axiom_root=project_root,
        )
        # `install --backend launchd` looks for .plist → finds nothing.
        # Bug: today this prints "(no schedule artifacts for host 'test-host')"
        # — silently exits 0 even though the user wrote a schedule and
        # asked for an install. Should fail loudly with guidance.
        result = _run_axi(
            ["install", "--host", "test-host", "--backend", "launchd", "--dry-run"],
            cwd=project_root, axiom_root=project_root,
        )
        # Today: returncode=0, stdout says "no schedule artifacts" — silently ok.
        # Desired: nonzero exit OR a warning that schedules exist in a
        # different backend's format.
        artifacts_present = list(
            (project_root / "deploy" / "test-host").glob("*")
        )
        assert artifacts_present, "test setup error — no artifact written"
        if "no schedule artifacts" in result.stdout:
            pytest.fail(
                f"`install --backend launchd` reported 'no schedule "
                f"artifacts' while {[a.name for a in artifacts_present]} "
                f"exist under deploy/test-host/ in a different backend's "
                f"format. Silent empty-install on user-written schedules "
                f"is a UX trap.\nstdout:\n{result.stdout}"
            )


class TestUninstallHonesty:
    """`axi schedule uninstall --host X --name N` shouldn't claim
    'uninstalled' when nothing was actually removed (schedule was
    never installed, or `--name` doesn't match anything)."""

    def test_uninstall_of_unknown_schedule_signals_not_found(self, project_root):
        # Don't create anything; just attempt uninstall.
        # Today: prints "uninstalled never-was" with rc=0 — misleading.
        result = _run_axi(
            ["uninstall", "--host", "test-host", "--name", "never-was",
             "--backend", "cron"],
            cwd=project_root, axiom_root=project_root,
        )
        # We tolerate either: explicit failure message, OR a clear
        # "not installed" / "nothing to remove" message. What we will
        # NOT tolerate is a confident "uninstalled" when nothing was
        # actually removed.
        if "uninstalled never-was" in result.stdout and "not" not in result.stdout.lower():
            pytest.fail(
                f"uninstall reported success for a schedule that was "
                f"never installed. stdout:\n{result.stdout}"
            )


class TestAutoDetectFindsCreatedArtifacts:
    """If `create` writes a `.cron` file but auto-detect on the host
    picks `systemd`, the orchestrator's `*.systemd` glob finds nothing
    — install reports "no schedule artifacts" while artifacts plainly
    exist. That's the silent-failure UX we have to surface as an
    error, not as success."""

    def test_create_then_auto_install_dry_run_finds_artifacts(
        self, project_root, monkeypatch,
    ):
        # Create a schedule for localhost (will auto-detect launchd on macOS)
        result = _run_axi(
            ["create", "dev-poll", "--host", "localhost",
             "--cron", "*/5 * * * *", "--command", "axi doctor --quiet"],
            cwd=project_root, axiom_root=project_root,
        )
        assert result.returncode == 0, result.stderr

        # Auto-detect dry-run install — should find the dev-poll schedule
        # (not "no schedule artifacts").
        # dry-run with --backend auto short-circuits to cron, so this
        # also covers the .cron-named artifact case.
        result = _run_axi(
            ["install", "--host", "localhost", "--dry-run"],
            cwd=project_root, axiom_root=project_root,
        )
        assert result.returncode == 0, result.stderr
        assert "dev-poll" in result.stdout, (
            f"auto-detect install --dry-run didn't find the schedule that "
            f"create just wrote:\n{result.stdout}"
        )
        assert "no schedule artifacts" not in result.stdout, (
            f"'create' and 'install' don't agree on artifact discovery:\n"
            f"{result.stdout}"
        )
