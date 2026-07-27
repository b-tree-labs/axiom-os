# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tests for `axiom.cli.scheduling.install_schedules` (issue #205, slice 3).

The orchestrator brings Backend + Runner + artifact discovery together:

  install_schedules(project_root, host, *, runner, backend) -> InstallReport

For each `<deploy_dir>/<host>/*.<backend.ext>` artifact:
  - parse via `backend.install_artifact(runner, schedule_name, content)`
  - wrap in try/except; per-schedule outcome
  - aggregate into InstallReport

The orchestrator stays backend-agnostic: it asks the backend for its
artifact filename suffix and lets the backend parse its own content.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest


@dataclass
class FakeRunner:
    host: str = "test-host"

    def run(self, command, *, input=None):
        from axiom.cli.scheduling.protocols import CompletedRun
        return CompletedRun("", "", 0)

    def write_file(self, remote_path, content):
        return None


@dataclass
class FakeBackend:
    """Records calls; can be told to raise for specific schedules."""

    name: str = "cron"
    raise_for: set[str] = field(default_factory=set)
    installed: list[tuple[str, str]] = field(default_factory=list)  # (name, content)

    def artifact_filename(self, schedule_name: str) -> str:
        return f"{schedule_name}.cron"

    def render(self, *, schedule_name, cron, command):
        return f"{cron} {command}  # axi schedule managed: {schedule_name}\n"

    def install_artifact(self, *, runner, schedule_name, artifact_content):
        if schedule_name in self.raise_for:
            raise RuntimeError(f"forced failure for {schedule_name}")
        self.installed.append((schedule_name, artifact_content))

    def install(self, **kwargs):  # unused but Protocol-required
        raise NotImplementedError("orchestrator uses install_artifact")

    def uninstall(self, *, runner, schedule_name):
        raise NotImplementedError("orchestrator doesn't uninstall here")


@pytest.fixture
def project_root(tmp_path: Path) -> Path:
    root = tmp_path / "project"
    root.mkdir()
    return root


def _write_artifact(project_root: Path, host: str, name: str, content: str = "x"):
    deploy = project_root / "deploy" / host
    deploy.mkdir(parents=True, exist_ok=True)
    (deploy / f"{name}.cron").write_text(content)


class TestEmptyDeployDir:
    def test_no_deploy_dir_returns_empty_report(self, project_root):
        from axiom.cli.scheduling import install_schedules
        rpt = install_schedules(
            project_root=project_root,
            host="test-host",
            runner=FakeRunner(),
            backend=FakeBackend(),
        )
        assert rpt.host == "test-host"
        assert rpt.backend == "cron"
        assert rpt.outcomes == []

    def test_no_host_dir_returns_empty_report(self, project_root):
        from axiom.cli.scheduling import install_schedules
        (project_root / "deploy" / "other-host").mkdir(parents=True)
        rpt = install_schedules(
            project_root=project_root,
            host="test-host",
            runner=FakeRunner(),
            backend=FakeBackend(),
        )
        assert rpt.outcomes == []


class TestInstallSchedules:
    def test_one_artifact_one_installed_outcome(self, project_root):
        from axiom.cli.scheduling import install_schedules
        _write_artifact(project_root, "test-host", "heartbeat", "*/15 * * * * echo")

        backend = FakeBackend()
        rpt = install_schedules(
            project_root=project_root,
            host="test-host",
            runner=FakeRunner(),
            backend=backend,
        )

        assert len(rpt.outcomes) == 1
        assert rpt.outcomes[0].schedule_name == "heartbeat"
        assert rpt.outcomes[0].status == "installed"
        assert rpt.all_ok is True
        # Backend received the artifact content verbatim
        assert backend.installed == [("heartbeat", "*/15 * * * * echo")]

    def test_multiple_artifacts_all_routed(self, project_root):
        from axiom.cli.scheduling import install_schedules
        for n in ("alpha", "beta", "gamma"):
            _write_artifact(project_root, "test-host", n)

        backend = FakeBackend()
        rpt = install_schedules(
            project_root=project_root,
            host="test-host",
            runner=FakeRunner(),
            backend=backend,
        )

        names = sorted(o.schedule_name for o in rpt.outcomes)
        assert names == ["alpha", "beta", "gamma"]
        assert rpt.all_ok is True

    def test_per_schedule_failure_isolated(self, project_root):
        """One schedule's install failing doesn't block the others."""
        from axiom.cli.scheduling import install_schedules
        for n in ("alpha", "beta", "gamma"):
            _write_artifact(project_root, "test-host", n)

        backend = FakeBackend(raise_for={"beta"})
        rpt = install_schedules(
            project_root=project_root,
            host="test-host",
            runner=FakeRunner(),
            backend=backend,
        )

        assert rpt.all_ok is False
        statuses = {o.schedule_name: o.status for o in rpt.outcomes}
        assert statuses == {
            "alpha": "installed",
            "beta": "failed",
            "gamma": "installed",
        }
        beta = next(o for o in rpt.outcomes if o.schedule_name == "beta")
        assert "forced failure for beta" in beta.error


class TestFilterByName:
    def test_name_filter_only_routes_named(self, project_root):
        from axiom.cli.scheduling import install_schedules
        for n in ("alpha", "beta", "gamma"):
            _write_artifact(project_root, "test-host", n)

        backend = FakeBackend()
        rpt = install_schedules(
            project_root=project_root,
            host="test-host",
            runner=FakeRunner(),
            backend=backend,
            name_filter="beta",
        )

        assert [o.schedule_name for o in rpt.outcomes] == ["beta"]
        assert [s for (s, _) in backend.installed] == ["beta"]

    def test_unknown_name_filter_returns_empty(self, project_root):
        from axiom.cli.scheduling import install_schedules
        _write_artifact(project_root, "test-host", "alpha")

        rpt = install_schedules(
            project_root=project_root,
            host="test-host",
            runner=FakeRunner(),
            backend=FakeBackend(),
            name_filter="nonexistent",
        )
        assert rpt.outcomes == []


class TestDryRun:
    def test_dry_run_discovers_but_does_not_install(self, project_root):
        from axiom.cli.scheduling import install_schedules
        _write_artifact(project_root, "test-host", "alpha")
        _write_artifact(project_root, "test-host", "beta")

        backend = FakeBackend()
        rpt = install_schedules(
            project_root=project_root,
            host="test-host",
            runner=FakeRunner(),
            backend=backend,
            dry_run=True,
        )

        # Outcomes reported (discoverable schedules) but with `skipped` status
        statuses = {o.schedule_name: o.status for o in rpt.outcomes}
        assert statuses == {"alpha": "skipped", "beta": "skipped"}
        # Backend.install_artifact NOT called
        assert backend.installed == []
        # Dry-run is "all_ok" since nothing failed
        assert rpt.all_ok is True
