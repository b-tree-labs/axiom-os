# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tests for `axi schedule install` / `axi schedule uninstall` CLI (issue #205, slice 5).

The existing `axi schedule create` / `axi schedule list` (from PR #204) live
in `axiom.cli.schedule`. This slice extends that same argparse surface
with `install` and `uninstall` actions that wrap the orchestrator
(slice 3) + the SchedulerBackend Protocol (slice 1).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest


@dataclass
class FakeRunner:
    host: str = "test-host"
    runs: list[tuple[Any, str | None]] = field(default_factory=list)

    def run(self, command, *, input=None):
        from axiom.cli.scheduling.protocols import CompletedRun
        self.runs.append((command, input))
        return CompletedRun("", "", 0)

    def write_file(self, remote_path, content):
        return None


@pytest.fixture
def project_root(tmp_path: Path, monkeypatch) -> Path:
    """Pin `get_project_root` to a tmp via AXIOM_ROOT."""
    root = tmp_path / "project"
    root.mkdir()
    (root / ".git").mkdir()
    monkeypatch.setenv("AXIOM_ROOT", str(root))
    return root


def _write_artifact(project_root: Path, host: str, name: str, content: str):
    deploy = project_root / "deploy" / host
    deploy.mkdir(parents=True, exist_ok=True)
    (deploy / f"{name}.cron").write_text(content)


@pytest.fixture
def stub_ssh_runner(monkeypatch):
    """Replace `SSHRunner(host=...)` with a `FakeRunner`. The CLI builds
    its runner from `--host`; tests capture the calls without touching
    the network."""
    captured: list[FakeRunner] = []

    def fake_ssh_runner(*, host: str):
        r = FakeRunner(host=host)
        captured.append(r)
        return r

    # The CLI module looks up SSHRunner via a private factory hook so
    # tests can replace it without monkey-patching subprocess.
    monkeypatch.setattr(
        "axiom.cli.schedule._ssh_runner_factory",
        fake_ssh_runner,
    )
    return captured


class TestInstallAction:
    def test_install_with_one_artifact_returns_zero(
        self, project_root, stub_ssh_runner, capsys,
    ):
        from axiom.cli.schedule import main
        _write_artifact(
            project_root, "test-host", "heartbeat",
            "*/15 * * * * echo  # axi schedule managed: heartbeat\n",
        )

        rc = main(["install", "--host", "test-host"])
        assert rc == 0
        out = capsys.readouterr().out
        assert "heartbeat" in out
        assert "installed" in out.lower()

    def test_install_no_artifacts_returns_zero_with_message(
        self, project_root, stub_ssh_runner, capsys,
    ):
        from axiom.cli.schedule import main
        rc = main(["install", "--host", "test-host"])
        assert rc == 0
        out = capsys.readouterr().out
        assert "no schedule" in out.lower() or "0 schedule" in out.lower()

    def test_install_partial_failure_returns_nonzero(
        self, project_root, monkeypatch, capsys,
    ):
        """One failed schedule → CLI exits non-zero so CI can fail."""
        from axiom.cli import schedule

        # Stub the orchestrator to return a mixed report.
        def fake_install_schedules(**kwargs):
            from axiom.cli.scheduling.protocols import (
                InstallOutcome,
                InstallReport,
            )
            return InstallReport(
                host=kwargs["host"],
                backend=kwargs["backend"].name,
                outcomes=[
                    InstallOutcome(schedule_name="a", status="installed"),
                    InstallOutcome(schedule_name="b", status="failed", error="boom"),
                ],
            )

        monkeypatch.setattr(schedule, "install_schedules", fake_install_schedules)
        # Stub SSH so we don't actually touch network
        monkeypatch.setattr(schedule, "_ssh_runner_factory", lambda *, host: FakeRunner(host=host))

        rc = schedule.main(["install", "--host", "test-host"])
        assert rc != 0
        err = capsys.readouterr().out + capsys.readouterr().err
        # Failure surfaced
        assert "b" in err and "boom" in err.lower() or "failed" in err.lower()


class TestDryRunAction:
    def test_dry_run_reports_skipped_without_apply(
        self, project_root, stub_ssh_runner, capsys,
    ):
        from axiom.cli.schedule import main
        _write_artifact(
            project_root, "test-host", "alpha",
            "0 * * * * echo  # axi schedule managed: alpha\n",
        )

        rc = main(["install", "--host", "test-host", "--dry-run"])
        assert rc == 0
        out = capsys.readouterr().out
        assert "skipped" in out.lower() or "dry" in out.lower()
        # Runner wasn't actually invoked
        assert all(r.runs == [] for r in stub_ssh_runner)


class TestNameFilter:
    def test_name_filter_routes_only_one(
        self, project_root, stub_ssh_runner, capsys,
    ):
        from axiom.cli.schedule import main
        for n in ("alpha", "beta"):
            _write_artifact(
                project_root, "test-host", n,
                f"0 * * * * echo  # axi schedule managed: {n}\n",
            )

        rc = main(["install", "--host", "test-host", "--name", "beta"])
        assert rc == 0
        out = capsys.readouterr().out
        assert "beta" in out
        assert "alpha" not in out


class TestBackendSelection:
    def test_default_backend_is_cron(
        self, project_root, stub_ssh_runner, capsys,
    ):
        from axiom.cli.schedule import main
        _write_artifact(
            project_root, "test-host", "x",
            "0 * * * * echo  # axi schedule managed: x\n",
        )
        rc = main(["install", "--host", "test-host"])
        assert rc == 0
        out = capsys.readouterr().out
        # Backend named in the report header
        assert "cron" in out.lower()

    def test_unknown_backend_rejected(
        self, project_root, stub_ssh_runner,
    ):
        from axiom.cli.schedule import main
        with pytest.raises(SystemExit) as excinfo:
            main(["install", "--host", "test-host", "--backend", "bogus"])
        # argparse exits 2 on bad choices
        assert excinfo.value.code == 2


class TestUninstallAction:
    def test_uninstall_invokes_backend(
        self, project_root, stub_ssh_runner, monkeypatch, capsys,
    ):
        from axiom.cli import schedule

        called: list[tuple] = []

        class StubBackend:
            name = "cron"

            def artifact_filename(self, schedule_name):
                return f"{schedule_name}.cron"

            def uninstall(self, *, runner, schedule_name):
                called.append((runner.host, schedule_name))

        # The uninstall CLI now requires an artifact to exist (existence
        # check before claiming success); lay one down.
        _write_artifact(
            project_root, "test-host", "heartbeat",
            "0 * * * * echo  # axi schedule managed: heartbeat\n",
        )

        # Replace the backend factory so we can capture the call.
        monkeypatch.setattr(schedule, "_backend_for", lambda name: StubBackend())

        rc = schedule.main(["uninstall", "--host", "test-host", "--name", "heartbeat"])
        assert rc == 0
        assert called == [("test-host", "heartbeat")]

    def test_uninstall_requires_name(self, project_root, stub_ssh_runner):
        from axiom.cli.schedule import main
        with pytest.raises(SystemExit) as excinfo:
            main(["uninstall", "--host", "test-host"])
        assert excinfo.value.code == 2
