# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tests for node-level health monitoring probes.

Unit tests use a fake `run` callable to simulate system command outputs.
Integration tests (marked @pytest.mark.integration) run real probes on the
host and are skipped by default — run with `pytest -m integration`.
"""

from __future__ import annotations

import os
import sys
import unittest.mock
from datetime import UTC, datetime, timedelta

import pytest

from axiom.extensions.builtins.hygiene.node_health import (
    Finding,
    JournalGap,
    NodeHealthReport,
    Severity,
    audit_node,
    check_cpu_governor,
    check_desktop_environment,
    check_gnome_suspend,
    check_kdump,
    check_max_cstate,
    check_rasdaemon,
    check_sleep_targets,
    detect_journal_gaps,
    parse_boot_list,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _StableVersionChecker:
    """Hermetic stand-in: reports no upstream drift, empty current version.

    Without this, ``audit_node`` hits the real VersionChecker (network +
    the installed package's version metadata), which leaks ambient state
    into the report — a `stale_version` finding appears whenever the dev's
    editable install lags HEAD, breaking otherwise-deterministic tests.
    """

    def check_remote_version(self, timeout: float = 3.0):
        from types import SimpleNamespace

        return SimpleNamespace(is_newer=False, current="", available="")

    def get_current_version(self) -> str:
        return ""


class _NoDirectives:
    """Hermetic stand-in: no active version directives."""

    def load_active(self) -> list:
        return []


def fake_run(responses: dict):
    """Return a run callable that returns canned responses keyed by command."""

    def _run(cmd, timeout=10):
        key = tuple(cmd)
        if key in responses:
            return responses[key]
        # Try matching by first element (command name)
        for k, v in responses.items():
            if k[0] == cmd[0]:
                return v
        return (-1, "")

    return _run


# ---------------------------------------------------------------------------
# Finding / Report data model
# ---------------------------------------------------------------------------


class TestDataModel:
    def test_finding_to_dict(self):
        f = Finding(
            check="test",
            severity=Severity.CRITICAL,
            message="bad thing",
            current_value="bad",
            expected_value="good",
            auto_fixable=True,
        )
        d = f.to_dict()
        assert d["severity"] == "critical"
        assert d["auto_fixable"] is True

    def test_report_healthy_when_empty(self):
        r = NodeHealthReport()
        assert r.healthy is True
        assert r.critical_count == 0
        assert r.warning_count == 0

    def test_report_unhealthy_with_critical(self):
        r = NodeHealthReport(
            findings=[
                Finding("a", Severity.CRITICAL, "bad"),
                Finding("b", Severity.WARNING, "meh"),
                Finding("c", Severity.INFO, "fyi"),
            ]
        )
        assert r.healthy is False
        assert r.critical_count == 1
        assert r.warning_count == 1

    def test_report_to_dict(self):
        r = NodeHealthReport()
        d = r.to_dict()
        assert "timestamp" in d
        assert d["healthy"] is True
        assert d["findings"] == []
        assert d["journal_gaps"] == []

    def test_journal_gap_to_dict(self):
        g = JournalGap(
            last_entry=datetime(2026, 3, 20, 5, 26, tzinfo=UTC),
            next_boot=datetime(2026, 3, 31, 16, 22, tzinfo=UTC),
            gap=timedelta(days=11, hours=10, minutes=56),
        )
        d = g.to_dict()
        assert d["gap_seconds"] == pytest.approx(11 * 86400 + 10 * 3600 + 56 * 60)


# ---------------------------------------------------------------------------
# GNOME suspend
# ---------------------------------------------------------------------------


class TestGnomeSuspend:
    def test_suspend_enabled(self):
        run = fake_run(
            {
                (
                    "gsettings",
                    "get",
                    "org.gnome.settings-daemon.plugins.power",
                    "sleep-inactive-ac-type",
                ): (0, "'suspend'"),
            }
        )
        f = check_gnome_suspend(run=run)
        assert f is not None
        assert f.severity == Severity.CRITICAL
        assert f.check == "gnome_suspend"
        assert f.auto_fixable is True

    def test_suspend_disabled(self):
        run = fake_run(
            {
                (
                    "gsettings",
                    "get",
                    "org.gnome.settings-daemon.plugins.power",
                    "sleep-inactive-ac-type",
                ): (0, "'nothing'"),
            }
        )
        f = check_gnome_suspend(run=run)
        assert f is None

    def test_hibernate_is_also_bad(self):
        run = fake_run(
            {
                (
                    "gsettings",
                    "get",
                    "org.gnome.settings-daemon.plugins.power",
                    "sleep-inactive-ac-type",
                ): (0, "'hibernate'"),
            }
        )
        f = check_gnome_suspend(run=run)
        assert f is not None
        assert f.severity == Severity.CRITICAL

    def test_no_gsettings(self):
        """Non-GNOME systems should return None, not an error."""
        run = fake_run({})
        f = check_gnome_suspend(run=run)
        assert f is None


# ---------------------------------------------------------------------------
# Sleep targets
# ---------------------------------------------------------------------------


class TestSleepTargets:
    def test_all_masked(self):
        def run(cmd, timeout=10):
            if cmd[0] == "systemctl" and cmd[1] == "is-enabled":
                return (0, "masked")
            return (-1, "")

        findings = check_sleep_targets(run=run)
        assert findings == []

    def test_some_unmasked(self):
        states = {
            "sleep.target": "static",
            "suspend.target": "static",
            "hibernate.target": "static",
            "hybrid-sleep.target": "masked",
            "suspend-then-hibernate.target": "masked",
        }

        def run(cmd, timeout=10):
            if cmd[0] == "systemctl" and cmd[1] == "is-enabled":
                target = cmd[2]
                return (0, states.get(target, "unknown"))
            return (-1, "")

        findings = check_sleep_targets(run=run)
        assert len(findings) == 3
        assert all(f.severity == Severity.CRITICAL for f in findings)
        targets_found = {f.current_value for f in findings}
        assert targets_found == {"static"}

    def test_systemctl_not_available(self):
        run = fake_run({})
        findings = check_sleep_targets(run=run)
        assert findings == []


# ---------------------------------------------------------------------------
# CPU governor
# ---------------------------------------------------------------------------


class TestCpuGovernor:
    def test_powersave(self):
        run = fake_run(
            {
                ("cat", "/sys/devices/system/cpu/cpufreq/policy0/scaling_governor"): (
                    0,
                    "powersave",
                ),
            }
        )
        f = check_cpu_governor(run=run)
        assert f is not None
        assert f.severity == Severity.WARNING
        assert f.check == "cpu_governor"
        assert "powersave" in f.current_value

    def test_performance_is_ok(self):
        run = fake_run(
            {
                ("cat", "/sys/devices/system/cpu/cpufreq/policy0/scaling_governor"): (
                    0,
                    "performance",
                ),
            }
        )
        f = check_cpu_governor(run=run)
        assert f is None

    def test_no_sysfs(self):
        run = fake_run({})
        f = check_cpu_governor(run=run)
        assert f is None


# ---------------------------------------------------------------------------
# C-state
# ---------------------------------------------------------------------------


class TestMaxCstate:
    def test_unrestricted(self):
        run = fake_run(
            {
                ("cat", "/proc/cmdline"): (0, "BOOT_IMAGE=/vmlinuz root=/dev/sda1 ro quiet splash"),
            }
        )
        f = check_max_cstate(run=run)
        assert f is not None
        assert f.severity == Severity.WARNING
        assert f.auto_fixable is False

    def test_restricted(self):
        run = fake_run(
            {
                ("cat", "/proc/cmdline"): (
                    0,
                    "BOOT_IMAGE=/vmlinuz root=/dev/sda1 processor.max_cstate=1 idle=nomwait",
                ),
            }
        )
        f = check_max_cstate(run=run)
        assert f is None

    def test_no_proc(self):
        run = fake_run({})
        f = check_max_cstate(run=run)
        assert f is None


# ---------------------------------------------------------------------------
# kdump
# ---------------------------------------------------------------------------


class TestKdump:
    def test_active(self):
        run = fake_run(
            {
                ("systemctl", "is-active", "kdump-tools"): (0, "active"),
            }
        )
        f = check_kdump(run=run)
        assert f is None

    def test_inactive(self):
        def run(cmd, timeout=10):
            if cmd[0] == "systemctl" and cmd[1] == "is-active":
                return (3, "inactive")
            return (-1, "")

        f = check_kdump(run=run)
        assert f is not None
        assert f.severity == Severity.WARNING
        assert f.check == "kdump"

    def test_rhel_naming(self):
        """kdump.service (RHEL) should also be accepted."""
        call_count = {"n": 0}

        def run(cmd, timeout=10):
            call_count["n"] += 1
            if cmd == ["systemctl", "is-active", "kdump-tools"]:
                return (3, "inactive")
            if cmd == ["systemctl", "is-active", "kdump"]:
                return (0, "active")
            return (-1, "")

        f = check_kdump(run=run)
        assert f is None


# ---------------------------------------------------------------------------
# rasdaemon
# ---------------------------------------------------------------------------


class TestRasdaemon:
    def test_active(self):
        run = fake_run(
            {
                ("systemctl", "is-active", "rasdaemon"): (0, "active"),
            }
        )
        f = check_rasdaemon(run=run)
        assert f is None

    def test_inactive(self):
        run = fake_run(
            {
                ("systemctl", "is-active", "rasdaemon"): (3, "inactive"),
            }
        )
        f = check_rasdaemon(run=run)
        assert f is not None
        assert f.severity == Severity.WARNING


# ---------------------------------------------------------------------------
# Desktop environment
# ---------------------------------------------------------------------------


class TestDesktopEnvironment:
    def test_gdm_running(self):
        def run(cmd, timeout=10):
            if cmd == ["systemctl", "is-active", "gdm"]:
                return (0, "active")
            return (3, "inactive")

        f = check_desktop_environment(run=run)
        assert f is not None
        assert f.severity == Severity.INFO
        assert "gdm" in f.current_value

    def test_no_dm_running(self):
        def run(cmd, timeout=10):
            return (3, "inactive")

        f = check_desktop_environment(run=run)
        assert f is None

    def test_sddm_running(self):
        def run(cmd, timeout=10):
            if cmd == ["systemctl", "is-active", "sddm"]:
                return (0, "active")
            return (3, "inactive")

        f = check_desktop_environment(run=run)
        assert f is not None
        assert "sddm" in f.current_value


# ---------------------------------------------------------------------------
# Journal gap analysis
# ---------------------------------------------------------------------------


# Realistic journalctl --list-boots output (modeled after a real headless host's
# reboot history — but with no identifying information)
SAMPLE_BOOT_LIST = """\
 -5 abc00001 Thu 2026-02-26 10:01:00 CDT—Thu 2026-03-05 16:00:00 CDT
 -4 abc00002 Thu 2026-03-05 16:00:30 CDT—Mon 2026-03-09 11:12:00 CDT
 -3 abc00003 Mon 2026-03-09 11:12:30 CDT—Tue 2026-03-10 12:55:00 CDT
 -2 abc00004 Tue 2026-03-10 12:55:30 CDT—Thu 2026-03-20 05:26:57 CDT
 -1 abc00005 Mon 2026-03-31 16:22:00 CDT—Tue 2026-04-01 12:00:00 CDT
  0 abc00006 Tue 2026-04-01 12:00:30 CDT—Tue 2026-04-01 13:00:00 CDT
"""


class TestParseBootList:
    def test_parses_all_boots(self):
        boots = parse_boot_list(SAMPLE_BOOT_LIST)
        assert len(boots) == 6

    def test_boot_indices(self):
        boots = parse_boot_list(SAMPLE_BOOT_LIST)
        indices = [b["index"] for b in boots]
        assert indices == [-5, -4, -3, -2, -1, 0]

    def test_empty_input(self):
        assert parse_boot_list("") == []

    def test_malformed_input(self):
        assert parse_boot_list("garbage line\nnonsense") == []


class TestDetectJournalGaps:
    def _ts(self, s: str) -> datetime:
        """Parse a timestamp string for testing."""
        for fmt in (
            "%a %Y-%m-%d %H:%M:%S %Z",
            "%Y-%m-%d %H:%M:%S",
        ):
            try:
                return datetime.strptime(s.strip(), fmt).replace(tzinfo=UTC)
            except ValueError:
                continue
        raise ValueError(f"Cannot parse: {s}")

    def test_detects_11_day_gap(self):
        """The big gap: boot -2 ended Mar 20 05:26, boot -1 started Mar 31 16:22.
        That's ~11.5 days — a clear hard freeze."""
        gaps = detect_journal_gaps(SAMPLE_BOOT_LIST)
        # Should find at least the 11-day gap
        big_gaps = [g for g in gaps if g.gap > timedelta(days=1)]
        assert len(big_gaps) >= 1
        biggest = max(big_gaps, key=lambda g: g.gap)
        assert biggest.gap > timedelta(days=10)

    def test_normal_reboots_not_flagged(self):
        """Quick reboots (< 30 min gap) should not be flagged."""
        boot_list = """\
 -1 aaa00001 Mon 2026-03-10 12:00:00 CDT—Mon 2026-03-10 12:55:00 CDT
  0 aaa00002 Mon 2026-03-10 12:55:30 CDT—Mon 2026-03-10 14:00:00 CDT
"""
        gaps = detect_journal_gaps(boot_list)
        assert gaps == []

    def test_custom_threshold(self):
        """With a very small threshold, even normal reboots are flagged."""
        boot_list = """\
 -1 aaa00001 Mon 2026-03-10 12:00:00 CDT—Mon 2026-03-10 12:55:00 CDT
  0 aaa00002 Mon 2026-03-10 13:30:00 CDT—Mon 2026-03-10 14:00:00 CDT
"""
        gaps = detect_journal_gaps(boot_list, gap_threshold=timedelta(minutes=5))
        assert len(gaps) == 1
        assert gaps[0].gap == timedelta(minutes=35)

    def test_single_boot(self):
        boot_list = " 0 aaa00001 Mon 2026-03-10 12:00:00 CDT—Mon 2026-03-10 14:00:00 CDT\n"
        gaps = detect_journal_gaps(boot_list)
        assert gaps == []


# ---------------------------------------------------------------------------
# Full audit
# ---------------------------------------------------------------------------


def _inert_corpus():
    """Facts that produce no findings, for tests asserting on OTHER checks.

    `audit_node` reaches the real retrieval store when not given facts — the
    same ambient-state leak `capability_registry` was made injectable for. A
    zero-row corpus short-circuits `run_all`, so these tests stay hermetic
    without pretending the collection does not happen in production.
    """
    from axiom.rag.storage_health import CorpusShape, StorageFacts

    return StorageFacts(
        indexes=[],
        shape=CorpusShape(0, 0, 0, "", 0),
        rows=0,
        ivfflat_lists=0,
        total_bytes=0,
        max_bytes=None,
        owner="",
        guard_enabled=True,
        shm_bytes=None,
    )


class TestAuditNode:
    def test_misconfigured_headless_host(self):
        """Simulate a host with the canonical headless-host misconfigurations:
        - GNOME suspend enabled
        - Sleep targets not masked (static)
        - Powersave governor
        - No max_cstate restriction
        - No kdump
        - rasdaemon running (it was)
        - GDM running
        - 11-day journal gap
        """

        def run(cmd, timeout=10):
            # gsettings
            if cmd[0] == "gsettings":
                return (0, "'suspend'")

            # systemctl is-enabled (sleep targets)
            if cmd[:2] == ["systemctl", "is-enabled"]:
                return (0, "static")

            # systemctl is-active
            if cmd[:2] == ["systemctl", "is-active"]:
                service = cmd[2]
                active = {"rasdaemon": True, "gdm": True}
                if active.get(service, False):
                    return (0, "active")
                return (3, "inactive")

            # /proc/cmdline (no max_cstate)
            if cmd == ["cat", "/proc/cmdline"]:
                return (
                    0,
                    "BOOT_IMAGE=/vmlinuz root=/dev/mapper/vg-root ro quiet splash vt.handoff=7",
                )

            # CPU governor
            if "scaling_governor" in str(cmd):
                return (0, "powersave")

            # journalctl --list-boots
            if cmd[0] == "journalctl":
                return (0, SAMPLE_BOOT_LIST)

            # Home dir + SSH (working on this host)
            if cmd[0] == "stat" and "-c" in cmd and "%U:%G" in cmd:
                return (0, "testuser:testuser")
            if cmd[0] == "test" and cmd[1] == "-d":
                return (0, "")
            if cmd[0] == "test" and cmd[1] == "-s":
                return (0, "")
            if cmd[0] == "stat" and "-c" in cmd and "%a" in cmd:
                return (0, "600")

            return (-1, "")

        with unittest.mock.patch.dict(os.environ, {"USER": "testuser"}):
            report = audit_node(run=run, boot_list_output=SAMPLE_BOOT_LIST)

        # Should NOT be healthy
        assert report.healthy is False

        checks_found = {f.check for f in report.findings}
        assert "gnome_suspend" in checks_found
        assert "sleep_target" in checks_found
        assert "max_cstate" in checks_found
        assert "kdump" in checks_found
        assert "desktop_environment" in checks_found

        # rasdaemon should NOT be a finding (it's running)
        assert "rasdaemon" not in checks_found

        # Should detect the 11-day gap
        assert len(report.journal_gaps) >= 1

        # Count severities
        assert report.critical_count >= 2  # gnome_suspend + sleep targets

    def test_healthy_host(self):
        """Simulate a properly configured host — all checks pass."""

        def run(cmd, timeout=10):
            if cmd[0] == "gsettings":
                return (0, "'nothing'")

            if cmd[:2] == ["systemctl", "is-enabled"]:
                return (0, "masked")

            if cmd[:2] == ["systemctl", "is-active"]:
                service = cmd[2]
                if service in ("kdump-tools", "rasdaemon"):
                    return (0, "active")
                return (3, "inactive")

            if cmd == ["cat", "/proc/cmdline"]:
                return (0, "BOOT_IMAGE=/vmlinuz root=/dev/sda1 processor.max_cstate=1 idle=nomwait")

            if "scaling_governor" in str(cmd):
                return (0, "performance")

            # Home dir ownership check
            if cmd[0] == "stat" and "-c" in cmd and "%U:%G" in cmd:
                return (0, "testuser:testuser")

            # SSH checks
            if cmd[0] == "test" and cmd[1] == "-d":
                return (0, "")  # directory exists
            if cmd[0] == "test" and cmd[1] == "-s":
                return (0, "")  # file exists and non-empty
            if cmd[0] == "stat" and "-c" in cmd and "%a" in cmd:
                return (0, "600")

            return (-1, "")

        # Clean boot list — no suspicious gaps
        clean_boots = """\
 -1 aaa00001 Mon 2026-03-31 16:22:00 CDT—Tue 2026-04-01 12:00:00 CDT
  0 aaa00002 Tue 2026-04-01 12:00:30 CDT—Tue 2026-04-01 13:00:00 CDT
"""
        with unittest.mock.patch.dict(os.environ, {"USER": "testuser"}):
            report = audit_node(
                run=run,
                boot_list_output=clean_boots,
                version_checker=_StableVersionChecker(),
                directive_store=_NoDirectives(),
                capability_registry=_AllDeclared(),
                corpus_facts=_inert_corpus(),
            )

        assert report.healthy is True
        assert report.critical_count == 0
        assert report.warning_count == 0
        assert report.findings == []
        assert report.journal_gaps == []

    def test_post_remediation_host(self):
        """Simulate the host AFTER fixes are applied and rebooted:
        - GNOME suspend disabled
        - Sleep targets masked
        - Performance governor
        - max_cstate=1 in cmdline
        - kdump active
        - rasdaemon active
        - GDM still running (operator hasn't removed it yet)
        """

        def run(cmd, timeout=10):
            if cmd[0] == "gsettings":
                return (0, "'nothing'")

            if cmd[:2] == ["systemctl", "is-enabled"]:
                return (0, "masked")

            if cmd[:2] == ["systemctl", "is-active"]:
                service = cmd[2]
                if service in ("kdump-tools", "rasdaemon", "gdm"):
                    return (0, "active")
                return (3, "inactive")

            if cmd == ["cat", "/proc/cmdline"]:
                return (
                    0,
                    "BOOT_IMAGE=/vmlinuz root=/dev/mapper/vg-root ro quiet splash processor.max_cstate=1 idle=nomwait",
                )

            if "scaling_governor" in str(cmd):
                return (0, "performance")

            if cmd[0] == "stat" and "-c" in cmd and "%U:%G" in cmd:
                return (0, "testuser:testuser")
            if cmd[0] == "test" and cmd[1] == "-d":
                return (0, "")
            if cmd[0] == "test" and cmd[1] == "-s":
                return (0, "")
            if cmd[0] == "stat" and "-c" in cmd and "%a" in cmd:
                return (0, "600")

            return (-1, "")

        clean_boots = """\
 -1 aaa00001 Tue 2026-04-01 14:00:00 CDT—Tue 2026-04-01 14:30:00 CDT
  0 aaa00002 Tue 2026-04-01 14:30:30 CDT—Tue 2026-04-01 15:00:00 CDT
"""
        with unittest.mock.patch.dict(os.environ, {"USER": "testuser"}):
            report = audit_node(
                run=run,
                boot_list_output=clean_boots,
                version_checker=_StableVersionChecker(),
                directive_store=_NoDirectives(),
                capability_registry=_AllDeclared(),
                corpus_facts=_inert_corpus(),
            )

        # Only GDM should remain as an INFO-level finding
        assert report.critical_count == 0
        assert report.warning_count == 0
        assert len(report.findings) == 1
        assert report.findings[0].check == "desktop_environment"
        assert report.findings[0].severity == Severity.INFO


class _AllDeclared:
    """A registry where every capability is declared.

    `audit_node` introspects the real extension set unless a registry is
    injected, so without this these hermetic tests would pass or fail on
    whatever happens to be installed on the machine running them — the exact
    ambient-state leak the injectable `version_checker` exists to prevent.
    """

    _skills: dict = {}
    _mutating: dict = {}

    def spec(self, name):  # pragma: no cover - never reached with no skills
        return object()


# ---------------------------------------------------------------------------
# Integration tests — run on a real host with `pytest -m integration`
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestIntegrationNodeHealth:
    """These tests run real system commands. Use `pytest -m integration` to
    include them. They are READ-ONLY and safe to run on any host."""

    @pytest.mark.skipif(sys.platform != "linux", reason="Linux only")
    def test_audit_runs_without_error(self):
        report = audit_node()
        assert isinstance(report, NodeHealthReport)
        assert report.timestamp is not None
        # We don't assert healthy/unhealthy — just that it runs
        d = report.to_dict()
        assert "findings" in d
        assert "journal_gaps" in d

    @pytest.mark.skipif(sys.platform != "linux", reason="Linux only")
    def test_findings_are_well_formed(self):
        report = audit_node()
        for f in report.findings:
            assert f.check
            assert f.severity in Severity
            assert f.message


# ---------------------------------------------------------------------------
# check_apt_keyrings — empty/missing signing keyrings (self-hosted node 2026-06-22)
# ---------------------------------------------------------------------------


def _apt_run(sources_out, sizes):
    """Fake run: `grep` returns sources_out; `stat -c %s <path>` looks up sizes.

    `sizes` maps keyring path -> (returncode, size_str). A path absent from
    the map simulates a missing file (stat returncode 1).
    """

    def _run(cmd, timeout=10):
        if cmd[0] == "grep":
            return (0, sources_out) if sources_out else (1, "")
        if cmd[0] == "stat":
            path = cmd[-1]
            return sizes.get(path, (1, ""))
        return (-1, "")

    return _run


class TestCheckAptKeyrings:
    def test_empty_keyring_flagged(self):
        from axiom.extensions.builtins.hygiene.node_health import check_apt_keyrings

        run = _apt_run(
            "signed-by=/etc/apt/keyrings/kubernetes-apt-keyring.gpg",
            {"/etc/apt/keyrings/kubernetes-apt-keyring.gpg": (0, "0")},
        )
        findings = check_apt_keyrings(run=run)
        assert len(findings) == 1
        assert findings[0].check == "apt_keyring"
        assert findings[0].severity == Severity.WARNING
        assert "kubernetes-apt-keyring.gpg" in findings[0].message
        assert findings[0].auto_fixable is False

    def test_missing_keyring_flagged(self):
        from axiom.extensions.builtins.hygiene.node_health import check_apt_keyrings

        run = _apt_run(
            "signed-by=/etc/apt/keyrings/gone.gpg",
            {},  # stat fails -> missing
        )
        findings = check_apt_keyrings(run=run)
        assert len(findings) == 1
        assert findings[0].current_value == "missing"

    def test_populated_keyring_ok(self):
        from axiom.extensions.builtins.hygiene.node_health import check_apt_keyrings

        run = _apt_run(
            "signed-by=/etc/apt/keyrings/docker.gpg",
            {"/etc/apt/keyrings/docker.gpg": (0, "2367")},
        )
        assert check_apt_keyrings(run=run) == []

    def test_no_signed_by_lines(self):
        from axiom.extensions.builtins.hygiene.node_health import check_apt_keyrings

        run = _apt_run("", {})
        assert check_apt_keyrings(run=run) == []

    def test_duplicate_paths_deduped(self):
        from axiom.extensions.builtins.hygiene.node_health import check_apt_keyrings

        run = _apt_run(
            "signed-by=/etc/apt/keyrings/k.gpg\nsigned-by=/etc/apt/keyrings/k.gpg",
            {"/etc/apt/keyrings/k.gpg": (0, "0")},
        )
        findings = check_apt_keyrings(run=run)
        assert len(findings) == 1


# ---------------------------------------------------------------------------
# Reclaimable-branches finding (2026-09-03): the heartbeat surfaces merged
# branches for approval, non-destructively (propose-on-heartbeat).
# ---------------------------------------------------------------------------


def _fake_run_repo_ok(cmd, *a, **k):
    if cmd[:2] == ["git", "rev-parse"]:
        return (0, "/tmp/repo")
    return (0, "")


class TestReclaimableBranchesFinding:
    def test_info_finding_when_merged_branches_exist(self):
        from axiom.extensions.builtins.hygiene.node_health import (
            Severity,
            check_reclaimable_branches,
        )

        f = check_reclaimable_branches(
            run=_fake_run_repo_ok,
            list_merged=lambda repo: ["feat/a", "feat/b"],
        )
        assert f is not None
        assert f.check == "reclaimable_branches"
        assert f.severity == Severity.INFO
        assert "2 merged" in f.message
        assert "feat/a" in f.current_value
        assert f.auto_fixable is False  # a proposal, not an auto-fix

    def test_none_when_no_candidates(self):
        from axiom.extensions.builtins.hygiene.node_health import check_reclaimable_branches

        assert check_reclaimable_branches(run=_fake_run_repo_ok, list_merged=lambda r: []) is None

    def test_none_when_repo_unresolved(self):
        from axiom.extensions.builtins.hygiene.node_health import check_reclaimable_branches

        def run(cmd, *a, **k):
            return (128, "")

        assert check_reclaimable_branches(run=run, list_merged=lambda r: ["x"]) is None

    def test_best_effort_swallows_errors(self):
        from axiom.extensions.builtins.hygiene.node_health import check_reclaimable_branches

        def boom(repo):
            raise RuntimeError("gh unavailable")

        assert check_reclaimable_branches(run=_fake_run_repo_ok, list_merged=boom) is None

    def test_audit_node_threads_and_surfaces_it(self):
        from axiom.extensions.builtins.hygiene.node_health import audit_node

        report = audit_node(
            run=_fake_run_repo_ok,
            version_checker=lambda *a, **k: None,
            reclaimable_branches=lambda repo: ["feat/done"],
            corpus_facts=_inert_corpus(),
        )
        assert "reclaimable_branches" in {f.check for f in report.findings}


class TestCorpusConfigurationOnTheHeartbeat:
    """The retrieval store's configuration drift has to actually ride the
    heartbeat. `vault steward` is the counterexample that motivated this: it
    existed, nobody invoked it, it was broken for weeks, and a credential
    expired. A check nobody runs is not a check.
    """

    def _facts(self, **over):
        from axiom.rag.storage_health import CorpusShape, IndexFacts, StorageFacts

        base = dict(
            indexes=[
                IndexFacts(
                    "idx_chunks_fts", "CREATE INDEX idx_chunks_fts ON chunks USING gin (x)", 1, 9
                )
            ],
            shape=CorpusShape(1000, 100, 10, "a.pdf", 20),
            rows=1000,
            ivfflat_lists=100,
            total_bytes=10,
            max_bytes=1_000_000,
            owner="@someone",
            guard_enabled=True,
            shm_bytes=1024 * 1024 * 1024,
        )
        base.update(over)
        return StorageFacts(**base)

    def test_audit_node_surfaces_a_corpus_configuration_finding(self):
        from axiom.extensions.builtins.hygiene.node_health import audit_node

        report = audit_node(
            run=_fake_run_repo_ok,
            version_checker=lambda *a, **k: None,
            reclaimable_branches=lambda repo: [],
            capability_registry={},
            corpus_report=None,
            corpus_facts=self._facts(guard_enabled=False),
        )
        checks = {f.check for f in report.findings}
        assert "rag_storage_ingest_guard" in checks

    def test_a_healthy_corpus_adds_nothing_to_the_report(self):
        """The negative control. If a healthy store still emits a finding, the
        heartbeat grows a permanent line nobody can clear."""
        from axiom.extensions.builtins.hygiene.node_health import audit_node

        report = audit_node(
            run=_fake_run_repo_ok,
            version_checker=lambda *a, **k: None,
            reclaimable_branches=lambda repo: [],
            capability_registry={},
            corpus_report=None,
            corpus_facts=self._facts(),
        )
        assert not [f for f in report.findings if f.check.startswith("rag_storage")]

    def test_an_unreachable_store_never_breaks_the_audit(self):
        """One subsystem's introspection must not take down the audit covering
        the rest, and must not report an unreachable database as an empty one —
        that looks like an answer."""
        from axiom.extensions.builtins.hygiene import node_health

        def _boom():
            raise RuntimeError("connection refused")

        original = node_health._collect_corpus_facts
        node_health._collect_corpus_facts = _boom
        try:
            assert node_health.check_corpus_configuration() == []
        finally:
            node_health._collect_corpus_facts = original


class TestCorpusFactCollectionActuallyConnects:
    """Found on the node, not in tests: the collector never fired.

    `_collect_corpus_facts` built a store and read `_conn` off it without ever
    calling `connect()`, so the attribute was always None, the function always
    returned None, and `check_corpus_configuration` always returned []. A check
    that cannot fire, reported as a clean bill of health — the exact inverse of
    the rule the storage_health module is written around.

    It was invisible because "could not measure" is deliberately silent, so the
    bug wore the same face as a healthy store. Only running the finding path
    against a real corpus showed it.

    Second defect in the same three lines: the store was built with
    schema-ensure ON, which runs CREATE TABLE. A monitoring check must never
    migrate a schema, and on the node it failed with InsufficientPrivilege
    against the read-only role that monitoring should use.
    """

    def test_the_store_is_built_read_only_and_connected(self):
        from axiom.extensions.builtins.hygiene import node_health as nh

        calls = {}

        class _Store:
            _conn = None

            def connect(self):
                calls["connected"] = True
                self._conn = _Conn()

        class _Conn:
            def cursor(self):
                raise AssertionError("not reached in this test")

        # Signature mirrors the real create_store EXACTLY. The first version of
        # this double took **kw, so it happily accepted a keyword the real
        # function did not have — the test passed and the node raised TypeError.
        def _create_store(database_url, *, ensure_schema=True):
            calls["ensure_schema"] = ensure_schema
            return _Store()

        import axiom.rag.store_factory as sf

        orig = sf.create_store
        sf.create_store = _create_store
        try:
            nh._collect_corpus_facts()
        except AssertionError:
            pass  # got as far as using the cursor, which is what we want
        except Exception:
            pass
        finally:
            sf.create_store = orig

        assert calls.get("connected") is True, "collector must call connect()"
        assert calls.get("ensure_schema") is False, (
            "a health check must not run schema DDL — it fails on the read-only "
            "role monitoring should use"
        )
