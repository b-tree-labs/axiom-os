# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Protocol tests for `axiom.cli.scheduling.protocols` (issue #205).

These are the load-bearing types every Backend + Runner impl conforms
to. Tests live up front so the Protocol shape is forced by concrete
expectations (cron + launchd + systemd-timer + Windows) before any
single impl gets a chance to leak its assumptions into the interface.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

import pytest


class TestCompletedRun:
    """A `CompletedRun` is what a `RemoteRunner.run()` returns."""

    def test_construction(self):
        from axiom.cli.scheduling.protocols import CompletedRun
        r = CompletedRun(stdout="hello\n", stderr="", returncode=0)
        assert r.stdout == "hello\n"
        assert r.stderr == ""
        assert r.returncode == 0

    def test_is_frozen(self):
        from axiom.cli.scheduling.protocols import CompletedRun
        r = CompletedRun(stdout="", stderr="", returncode=0)
        with pytest.raises(Exception):
            r.returncode = 1  # type: ignore[misc]

    def test_ok_predicate(self):
        from axiom.cli.scheduling.protocols import CompletedRun
        assert CompletedRun("", "", 0).ok is True
        assert CompletedRun("", "boom", 1).ok is False


class TestRemoteRunnerProtocol:
    """`RemoteRunner` is the transport abstraction. SSH today; WinRM
    later. The Protocol stays narrow — `run` + `write_file` — so
    multiple transports can stand up against the same Backend impls."""

    def test_protocol_has_required_methods(self):
        from axiom.cli.scheduling.protocols import RemoteRunner
        assert hasattr(RemoteRunner, "run")
        assert hasattr(RemoteRunner, "write_file")
        # host attribute for messaging + idempotence keying
        assert "host" in getattr(RemoteRunner, "__annotations__", {})

    def test_protocol_is_runtime_checkable(self):
        """Concrete impls (SSHRunner, WinRMRunner, MockRunner) need to
        satisfy `isinstance(runner, RemoteRunner)` for orchestrator
        dispatch."""
        from axiom.cli.scheduling.protocols import RemoteRunner

        class Stub:
            host: str = "x"

            def run(self, command, *, input=None):
                from axiom.cli.scheduling.protocols import CompletedRun
                return CompletedRun("", "", 0)

            def write_file(self, remote_path, content):
                return None

        assert isinstance(Stub(), RemoteRunner)


class TestSchedulerBackendProtocol:
    """`SchedulerBackend` declares the per-OS install lifecycle.
    Conformers: `CronBackend`, `LaunchdBackend`, eventually
    `SystemdTimerBackend` + `WindowsTaskSchedulerBackend`."""

    def test_protocol_has_required_methods(self):
        from axiom.cli.scheduling.protocols import SchedulerBackend
        for method in ("artifact_filename", "render", "install", "uninstall"):
            assert hasattr(SchedulerBackend, method), f"missing {method}"

    def test_protocol_has_name_attr(self):
        """`backend.name` keys log messages, CLI selection, registry."""
        from axiom.cli.scheduling.protocols import SchedulerBackend
        assert "name" in getattr(SchedulerBackend, "__annotations__", {})


class TestInstallReport:
    """`InstallReport` is the per-schedule outcome the orchestrator
    returns. Allows partial-failure reporting across many schedules
    on one host."""

    def test_construction_success(self):
        from axiom.cli.scheduling.protocols import InstallOutcome, InstallReport
        rpt = InstallReport(
            host="test-host",
            backend="cron",
            outcomes=[
                InstallOutcome(schedule_name="heartbeat", status="installed"),
            ],
        )
        assert rpt.host == "test-host"
        assert rpt.all_ok is True

    def test_partial_failure(self):
        from axiom.cli.scheduling.protocols import InstallOutcome, InstallReport
        rpt = InstallReport(
            host="test-host",
            backend="cron",
            outcomes=[
                InstallOutcome(schedule_name="a", status="installed"),
                InstallOutcome(schedule_name="b", status="failed", error="oops"),
            ],
        )
        assert rpt.all_ok is False
        # Error message threaded for human review
        failed = [o for o in rpt.outcomes if o.status == "failed"]
        assert failed[0].error == "oops"

    def test_status_values_constrained(self):
        """Limited vocabulary: 'installed' | 'unchanged' | 'failed' |
        'skipped'. 'unchanged' = idempotent re-install caught a no-op."""
        from axiom.cli.scheduling.protocols import InstallOutcome
        # All four should construct without error
        for status in ("installed", "unchanged", "failed", "skipped"):
            InstallOutcome(schedule_name="x", status=status)
        # Anything else should raise
        with pytest.raises(ValueError):
            InstallOutcome(schedule_name="x", status="bogus")
