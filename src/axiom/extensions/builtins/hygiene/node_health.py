# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Node-level health monitoring for TIDY.

Layer 2 (deterministic, no LLM) — probes the host for misconfigurations,
thermal issues, power management problems, and journal anomalies that
indicate hard freezes.
"""

from __future__ import annotations

import enum
import re
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------


class Severity(str, enum.Enum):
    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"


@dataclass
class Finding:
    """A single misconfiguration or health issue detected on the host."""

    check: str
    severity: Severity
    message: str
    current_value: str = ""
    expected_value: str = ""
    auto_fixable: bool = False

    def to_dict(self) -> dict:
        return {
            "check": self.check,
            "severity": self.severity.value,
            "message": self.message,
            "current_value": self.current_value,
            "expected_value": self.expected_value,
            "auto_fixable": self.auto_fixable,
        }


@dataclass
class JournalGap:
    """A gap in journal timestamps indicating a hard freeze or unclean shutdown."""

    last_entry: datetime
    next_boot: datetime
    gap: timedelta

    def to_dict(self) -> dict:
        return {
            "last_entry": self.last_entry.isoformat(),
            "next_boot": self.next_boot.isoformat(),
            "gap_seconds": self.gap.total_seconds(),
        }


@dataclass
class NodeHealthReport:
    """Aggregate result of all node health probes."""

    timestamp: datetime = field(default_factory=lambda: datetime.now(UTC))
    findings: list[Finding] = field(default_factory=list)
    journal_gaps: list[JournalGap] = field(default_factory=list)

    @property
    def critical_count(self) -> int:
        return sum(1 for f in self.findings if f.severity == Severity.CRITICAL)

    @property
    def warning_count(self) -> int:
        return sum(1 for f in self.findings if f.severity == Severity.WARNING)

    @property
    def healthy(self) -> bool:
        return self.critical_count == 0 and self.warning_count == 0

    def to_dict(self) -> dict:
        return {
            "timestamp": self.timestamp.isoformat(),
            "healthy": self.healthy,
            "critical_count": self.critical_count,
            "warning_count": self.warning_count,
            "findings": [f.to_dict() for f in self.findings],
            "journal_gaps": [g.to_dict() for g in self.journal_gaps],
        }


# ---------------------------------------------------------------------------
# Shell helper
# ---------------------------------------------------------------------------


def _run(cmd: list[str], timeout: int = 10) -> tuple[int, str]:
    """Run a command and return (returncode, stdout). Never raises."""
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return result.returncode, result.stdout.strip()
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return -1, ""


# ---------------------------------------------------------------------------
# Individual probes
# ---------------------------------------------------------------------------


def check_gnome_suspend(run=_run) -> Finding | None:
    """Check if GNOME is configured to suspend on AC idle."""
    rc, out = run(
        [
            "gsettings",
            "get",
            "org.gnome.settings-daemon.plugins.power",
            "sleep-inactive-ac-type",
        ]
    )
    if rc != 0:
        return None  # gsettings not available — not a GNOME system

    value = out.strip().strip("'\"")
    if value != "nothing":
        return Finding(
            check="gnome_suspend",
            severity=Severity.CRITICAL,
            message="GNOME is configured to suspend on AC idle — "
            "this will cause hard freezes on server workloads",
            current_value=value,
            expected_value="nothing",
            auto_fixable=True,
        )
    return None


def check_sleep_targets(run=_run) -> list[Finding]:
    """Check if systemd sleep/suspend/hibernate targets are masked."""
    if run is _run and sys.platform != "linux":
        return []

    findings = []
    targets = [
        "sleep.target",
        "suspend.target",
        "hibernate.target",
        "hybrid-sleep.target",
        "suspend-then-hibernate.target",
    ]
    for target in targets:
        rc, out = run(["systemctl", "is-enabled", target])
        if rc == -1:
            continue
        state = out.strip()
        # masked or static are acceptable; anything else is a risk
        if state not in ("masked", "masked-runtime"):
            findings.append(
                Finding(
                    check="sleep_target",
                    severity=Severity.CRITICAL,
                    message=f"{target} is not masked — system may suspend/hibernate unexpectedly",
                    current_value=state,
                    expected_value="masked",
                    auto_fixable=True,
                )
            )
    return findings


def check_cpu_governor(run=_run) -> Finding | None:
    """Check if CPU scaling governor is set to performance."""
    rc, governor = run(["cat", "/sys/devices/system/cpu/cpufreq/policy0/scaling_governor"])
    if rc != 0:
        return None
    governor = governor.strip()

    if governor != "performance":
        return Finding(
            check="cpu_governor",
            severity=Severity.WARNING,
            message=f"CPU governor is '{governor}' — compute workloads benefit "
            "from 'performance' governor",
            current_value=governor,
            expected_value="performance",
            auto_fixable=True,
        )
    return None


def check_max_cstate(run=_run) -> Finding | None:
    """Check kernel cmdline for processor.max_cstate restriction."""
    rc, cmdline = run(["cat", "/proc/cmdline"])
    if rc != 0:
        return None
    cmdline = cmdline.strip()

    # Look for processor.max_cstate=1 or max_cstate=0
    if "processor.max_cstate=1" in cmdline or "processor.max_cstate=0" in cmdline:
        return None

    return Finding(
        check="max_cstate",
        severity=Severity.WARNING,
        message="Deep C-states are not restricted — on certain hardware/GPU "
        "combinations this can cause unrecoverable hangs",
        current_value="unrestricted",
        expected_value="processor.max_cstate=1 in kernel cmdline",
        auto_fixable=False,  # requires GRUB change + reboot
    )


def check_kdump(run=_run) -> Finding | None:
    """Check if kdump/crash dump tooling is installed and active."""
    if run is _run and sys.platform != "linux":
        return None

    rc, out = run(["systemctl", "is-active", "kdump-tools"])
    if rc == 0 and out.strip() == "active":
        return None

    # Also check for kdump.service (RHEL/Fedora naming)
    rc2, out2 = run(["systemctl", "is-active", "kdump"])
    if rc2 == 0 and out2.strip() == "active":
        return None

    return Finding(
        check="kdump",
        severity=Severity.WARNING,
        message="Crash dump tools (kdump) are not active — kernel panics "
        "will not be captured for post-mortem analysis",
        current_value="not active",
        expected_value="active",
        auto_fixable=True,
    )


def check_rasdaemon(run=_run) -> Finding | None:
    """Check if hardware error logging (rasdaemon) is running."""
    if run is _run and sys.platform != "linux":
        return None

    rc, out = run(["systemctl", "is-active", "rasdaemon"])
    if rc == 0 and out.strip() == "active":
        return None

    return Finding(
        check="rasdaemon",
        severity=Severity.WARNING,
        message="Hardware error logging (rasdaemon) is not running — "
        "memory/CPU/PCIe errors will not be recorded",
        current_value="not active",
        expected_value="active",
        auto_fixable=True,
    )


def check_desktop_environment(run=_run) -> Finding | None:
    """Check if a desktop environment is running on a headless workload."""
    if run is _run and sys.platform != "linux":
        return None

    display_managers = ["gdm", "gdm3", "sddm", "lightdm"]
    for dm in display_managers:
        rc, out = run(["systemctl", "is-active", dm])
        if rc == 0 and out.strip() == "active":
            return Finding(
                check="desktop_environment",
                severity=Severity.INFO,
                message=f"Display manager '{dm}' is running — consider "
                "disabling if this node has no attached display",
                current_value=f"{dm} active",
                expected_value="disabled for headless workloads",
                auto_fixable=False,  # operator decision
            )
    return None


# ---------------------------------------------------------------------------
# SSH / home directory probes (lessons #8, #9)
# ---------------------------------------------------------------------------


def check_home_dir_ownership(run=_run) -> Finding | None:
    """Check that the user's home directory has correct ownership.

    Wrong ownership (e.g. group=root) causes sshd to silently reject
    key-based auth.
    """
    if run is _run and sys.platform != "linux":
        return None

    import os  # pylint: disable=import-outside-toplevel

    home = os.path.expanduser("~")
    rc, out = run(["stat", "-c", "%U:%G", home])
    if rc != 0:
        return None

    owner_group = out.strip()
    user = os.environ.get("USER", "")
    if not user:
        return None

    # Expected: user:user (owner and group match username)
    expected = f"{user}:{user}"
    if owner_group != expected:
        return Finding(
            check="home_dir_ownership",
            severity=Severity.CRITICAL,
            message=(
                f"Home directory ownership is {owner_group} — "
                "sshd will silently reject key auth if group is not the user's primary group"
            ),
            current_value=owner_group,
            expected_value=expected,
            auto_fixable=True,
        )
    return None


def check_authorized_keys(run=_run) -> Finding | None:
    """Check that ~/.ssh/authorized_keys exists and is non-empty."""
    if run is _run and sys.platform != "linux":
        return None

    import os  # pylint: disable=import-outside-toplevel

    ssh_dir = os.path.expanduser("~/.ssh")
    auth_keys = os.path.join(ssh_dir, "authorized_keys")

    # Check .ssh directory exists
    rc, _ = run(["test", "-d", ssh_dir])
    if rc != 0:
        return Finding(
            check="authorized_keys",
            severity=Severity.CRITICAL,
            message="~/.ssh directory does not exist — SSH key auth is impossible",
            current_value="missing",
            expected_value="directory exists with correct permissions",
            auto_fixable=True,
        )

    # Check authorized_keys file
    rc, out = run(["test", "-s", auth_keys])
    if rc != 0:
        return Finding(
            check="authorized_keys",
            severity=Severity.WARNING,
            message=("~/.ssh/authorized_keys is missing or empty — remote key-based SSH will fail"),
            current_value="missing or empty",
            expected_value="non-empty file with authorized public keys",
            auto_fixable=False,
        )

    # Check permissions (should be 600 or 644)
    rc, perms = run(["stat", "-c", "%a", auth_keys])
    if rc == 0:
        perms = perms.strip()
        if perms not in ("600", "644", "640"):
            return Finding(
                check="authorized_keys_perms",
                severity=Severity.WARNING,
                message=f"~/.ssh/authorized_keys has permissions {perms} — sshd may reject it",
                current_value=perms,
                expected_value="600",
                auto_fixable=True,
            )

    return None


# ---------------------------------------------------------------------------
# Legacy mechanism detection
# ---------------------------------------------------------------------------


def check_agent_services(run=_run) -> list[Finding]:
    """Check that daemon agents are registered as system services.

    If an agent has startup='daemon' in its TOML but no corresponding
    systemd timer/launchd plist exists, that's a gap.
    """
    # Only run this check when using the real _run (not in mocked tests)
    # and only on platforms with service managers
    if run is not _run:
        return []  # Mocked tests handle their own assertions
    if sys.platform not in ("linux", "darwin"):
        return []

    findings = []

    try:
        from axiom.extensions.builtins.agents.cli import _discover_agent_extensions
    except ImportError:
        return findings

    daemon_agents = [
        ext for ext in _discover_agent_extensions() if ext.agent and ext.agent.is_always_on
    ]

    for ext in daemon_agents:
        # Agents that have no heartbeat_command shouldn't be expected to
        # be registered — that would crash-loop them. Flag the missing
        # heartbeat_command itself as the finding (lower severity: info).
        if not ext.agent.is_registrable:
            findings.append(
                Finding(
                    check="agent_heartbeat_command_missing",
                    severity=Severity.INFO,
                    message=(
                        f"Agent '{ext.name}' has startup=daemon but no "
                        "heartbeat_command declared — not registered."
                    ),
                    current_value="heartbeat_command=''",
                    expected_value='heartbeat_command="<noun> <subcommand> ..."',
                    auto_fixable=False,
                )
            )
            continue

        if sys.platform == "linux":
            # New code path registers a `.timer` for periodic agents + a
            # Type=oneshot `.service` it triggers. Legacy path used
            # `neut-{name}-heartbeat.timer`. Probe new first, fall back to
            # legacy so this check works across the transition.
            new_timer = f"neut-{ext.name}-agent.timer"
            legacy_timer = f"neut-{ext.name}-heartbeat.timer"

            rc_new, out_new = run(["systemctl", "--user", "is-active", new_timer])
            rc_legacy, out_legacy = run(["systemctl", "--user", "is-active", legacy_timer])

            new_active = rc_new == 0 and "active" in out_new
            legacy_active = rc_legacy == 0 and "active" in out_legacy

            if not (new_active or legacy_active):
                findings.append(
                    Finding(
                        check="agent_service_missing",
                        severity=Severity.WARNING,
                        message=(
                            f"Agent '{ext.name}' has startup=daemon but no systemd timer registered"
                        ),
                        current_value="not registered",
                        expected_value=f"{new_timer} active",
                        auto_fixable=True,
                    )
                )
            elif legacy_active and not new_active:
                # Transitional: node still on the legacy heartbeat pattern.
                # Nudge them to re-register so they pick up the new design
                # (which handles pip upgrades without a restart).
                findings.append(
                    Finding(
                        check="agent_legacy_heartbeat_unit",
                        severity=Severity.INFO,
                        message=(
                            f"Agent '{ext.name}' is registered via legacy "
                            f"{legacy_timer}; run `axi agents register` to "
                            "migrate to the new .timer + oneshot pattern."
                        ),
                        current_value=legacy_timer,
                        expected_value=new_timer,
                        auto_fixable=True,
                    )
                )

        elif sys.platform == "darwin":
            import os

            plist = os.path.expanduser(f"~/Library/LaunchAgents/com.axiom.{ext.name}-agent.plist")
            if not os.path.exists(plist):
                findings.append(
                    Finding(
                        check="agent_service_missing",
                        severity=Severity.WARNING,
                        message=f"Agent '{ext.name}' has startup=daemon but no launchd plist registered",
                        current_value="not registered",
                        expected_value=f"com.axiom.{ext.name}-agent.plist",
                        auto_fixable=True,
                    )
                )

    return findings


def check_stale_version(version_checker=None, directive_store=None) -> list[Finding]:
    """Detect local version drift and version-directive non-compliance.

    Two kinds of drift:
      - Ambient: newer version available upstream. Info-level for patch drift,
        warning for minor-or-greater drift (operator likely missed a release).
      - Directive-driven: an active version directive exists requiring a
        minimum version and the local install is below it. Severity follows
        the directive, default WARNING.

    Pure-check: returns findings; doesn't block, doesn't phone home beyond
    the version_checker's own cached lookup. Both args are injectable for
    testing.
    """
    findings: list[Finding] = []

    # --- ambient upstream drift
    try:
        if version_checker is None:
            from axiom.extensions.builtins.update.version_check import VersionChecker

            version_checker = VersionChecker()
        info = version_checker.check_remote_version(timeout=3.0)
        if info.is_newer and info.current and info.available:
            cur_parts = _parse_version(info.current)
            new_parts = _parse_version(info.available)
            if cur_parts and new_parts:
                # Same major+minor → patch drift (info). Otherwise warning.
                is_patch_only = cur_parts[:2] == new_parts[:2]
                severity = Severity.INFO if is_patch_only else Severity.WARNING
                findings.append(
                    Finding(
                        check="stale_version",
                        severity=severity,
                        message=(
                            f"Newer version available ({info.current} → {info.available}). "
                            "Run `axi update --check` to see changes."
                        ),
                        current_value=info.current,
                        expected_value=info.available,
                        auto_fixable=True,
                    )
                )
    except Exception:
        pass  # Never fail the whole health report because of network issues

    # --- directive-driven drift
    try:
        if directive_store is None:
            from axiom.policy.version_directive_store import load_active

            directives = load_active()
        else:
            directives = directive_store.load_active()
    except Exception:
        directives = []

    if directives:
        try:
            # Re-use version_checker's current version if we already have one
            from axiom.extensions.builtins.update.version_check import VersionChecker

            current = (version_checker or VersionChecker()).get_current_version()
        except Exception:
            current = ""

        cur_parts = _parse_version(current) if current else None
        for d in directives:
            min_parts = _parse_version(d.min_version)
            if not cur_parts or not min_parts:
                continue
            if cur_parts < min_parts:
                findings.append(
                    Finding(
                        check="version_directive_violation",
                        severity=Severity.WARNING,
                        message=(
                            f"Version directive from {d.issuer} requires "
                            f"{d.package} >= {d.min_version}"
                            + (f" by {d.deadline}" if d.deadline else "")
                            + f"; local is {current or 'unknown'}."
                        ),
                        current_value=current or "unknown",
                        expected_value=f">={d.min_version}",
                        auto_fixable=True,
                    )
                )

    return findings


def _parse_version(v: str) -> tuple[int, ...] | None:
    """Parse 'X.Y.Z' into a tuple of ints for comparison. None on failure."""
    try:
        parts = [int(x) for x in re.split(r"[.\-+]", v) if x.isdigit()]
        return tuple(parts) if parts else None
    except Exception:
        return None


def check_legacy_cron(run=_run) -> list[Finding]:
    """Detect legacy cron jobs that conflict with agent systemd services.

    When agents run via systemd, stale cron entries cause double execution.
    """
    findings = []
    rc, out = run(["crontab", "-l"])
    if rc != 0:
        return findings  # No crontab — clean

    legacy_patterns = [
        ("tidy-heartbeat", "Tidy heartbeat cron conflicts with neut-tidy-heartbeat.timer"),
        ("neut tidy", "Tidy cron conflicts with agent systemd service"),
        ("axi hygiene", "Tidy cron conflicts with agent systemd service"),
        ("neut serve", "Serve cron conflicts with neut-serve.service"),
    ]

    for pattern, message in legacy_patterns:
        if pattern.lower() in out.lower():
            findings.append(
                Finding(
                    check="legacy_cron",
                    severity=Severity.WARNING,
                    message=message,
                    current_value=f"crontab contains '{pattern}'",
                    expected_value="removed (systemd timer handles this)",
                    auto_fixable=True,
                )
            )

    return findings


# ---------------------------------------------------------------------------
# Port conflict detection
# ---------------------------------------------------------------------------

# Ports that Axiom infrastructure uses
_EXPECTED_PORTS = {
    5432: ("PostgreSQL", "K3D"),
    8080: ("LLM server", "K3D"),
    8766: ("neut serve", "systemd/launchd"),
    11434: ("Ollama", "launchd"),
}


def check_port_conflicts(run=_run) -> list[Finding]:
    """Check for port conflicts on infrastructure ports."""
    import socket

    findings = []
    for port, (service, expected_owner) in _EXPECTED_PORTS.items():
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.settimeout(0.5)
                result = s.connect_ex(("127.0.0.1", port))
                if result == 0:
                    # Port is in use — that's expected for running services
                    pass
                # Port is free — only a problem if we expect it to be running
        except OSError:
            pass

    # Check for unexpected listeners on our ports
    rc, out = run(["lsof", "-i", "-P", "-n"])
    if rc != 0:
        return findings

    # Parse lsof output for port conflicts
    for line in out.splitlines():
        if "LISTEN" not in line:
            continue
        for port, (service, _) in _EXPECTED_PORTS.items():
            if f":{port} " in line or f":{port}\n" in line or line.endswith(f":{port}"):
                # Port is in use — check if it's the expected process
                parts = line.split()
                if parts:
                    proc_name = parts[0].lower()
                    # Flag unexpected listeners
                    if port == 5432 and "postgres" in proc_name and "docker" not in proc_name:
                        findings.append(
                            Finding(
                                check="port_conflict",
                                severity=Severity.WARNING,
                                message=f"Native PostgreSQL on port {port} may conflict with K3D PostgreSQL",
                                current_value=proc_name,
                                expected_value="docker (K3D)",
                            )
                        )

    return findings


def check_service_installations(run=_run) -> list[Finding]:
    """Check for stale or conflicting service registrations.

    Skipped under tests (when `run` is a mock, not the real `_run`)
    because the implementation walks the user's actual
    `~/Library/LaunchAgents/` and stat()s referenced binaries —
    which would surface real findings even with everything else mocked.
    """
    if run is not _run or sys.platform != "darwin":
        return []  # launchd-specific; skip under test mocks

    findings = []

    # Check for stale launchd plists
    import os

    launch_agents = os.path.expanduser("~/Library/LaunchAgents")
    if os.path.isdir(launch_agents):
        for plist in os.listdir(launch_agents):
            if not any(prefix in plist for prefix in ("com.axiom.", "com.neutron")):
                continue
            # Check if the binary referenced in the plist actually exists
            plist_path = os.path.join(launch_agents, plist)
            try:
                with open(plist_path) as f:
                    content = f.read()
                # Simple check: look for the binary path
                import re as _re

                binaries = _re.findall(r"<string>(/[^<]+)</string>", content)
                for binary in binaries:
                    if binary.startswith("/") and not os.path.exists(binary):
                        findings.append(
                            Finding(
                                check="stale_service",
                                severity=Severity.WARNING,
                                message=f"Service {plist} references missing binary: {binary}",
                                current_value=binary,
                                expected_value="binary should exist",
                                auto_fixable=True,
                            )
                        )
            except OSError:
                pass

    return findings


# ---------------------------------------------------------------------------
# Journal gap analysis
# ---------------------------------------------------------------------------

_BOOT_LINE_RE = re.compile(
    r"^(\S+)\s+(\S+\s+\S+\s+\S+\s+\S+\s+\S+)\s+",
)


def parse_boot_list(boot_list_output: str) -> list[dict]:
    """Parse `journalctl --list-boots` output into structured data.

    Each line looks like:
       -3 <boot-id> Thu 2026-03-10 12:55:00 CDT—Thu 2026-03-10 13:16:00 CDT
    We extract the boot index and the start timestamp.
    """
    boots = []
    for line in boot_list_output.strip().splitlines():
        line = line.strip()
        if not line:
            continue
        # Split on the em-dash (—) to separate start and end times
        parts = line.split("—")
        if len(parts) < 2:
            parts = line.split("--")
        if len(parts) < 2:
            continue

        # The start portion: "  -3 <boot-id> Thu 2026-03-10 12:55:00 CDT"
        start_part = parts[0].strip()
        # The end portion: "Thu 2026-03-10 13:16:00 CDT"
        end_part = parts[-1].strip()

        tokens = start_part.split()
        if len(tokens) < 6:
            continue

        try:
            boot_index = int(tokens[0])
        except ValueError:
            continue

        boot_id = tokens[1]

        # Start timestamp: tokens[2:] is like "Thu 2026-03-10 12:55:00 CDT"
        start_str = " ".join(tokens[2:])

        # End timestamp
        end_str = end_part.strip()

        boots.append(
            {
                "index": boot_index,
                "boot_id": boot_id,
                "start_str": start_str,
                "end_str": end_str,
            }
        )

    return boots


def detect_journal_gaps(
    boot_list_output: str,
    gap_threshold: timedelta = timedelta(minutes=30),
    parse_timestamp=None,
) -> list[JournalGap]:
    """Detect suspicious gaps between the end of one boot and start of the next.

    A gap larger than `gap_threshold` between the last log entry of boot N
    and the first log entry of boot N+1 suggests a hard freeze (the system
    was unresponsive for that duration before being power-cycled).
    """
    boots = parse_boot_list(boot_list_output)
    if len(boots) < 2:
        return []

    # Sort by index ascending (most negative = oldest)
    boots.sort(key=lambda b: b["index"])

    if parse_timestamp is None:

        def parse_timestamp(s: str) -> datetime | None:
            s = s.strip()
            # Strip trailing timezone abbreviation (CDT, CST, UTC, etc.)
            # — strptime %Z handling is unreliable across platforms
            s = re.sub(r"\s+[A-Z]{2,5}$", "", s)
            for fmt in (
                "%a %Y-%m-%d %H:%M:%S",
                "%Y-%m-%d %H:%M:%S",
            ):
                try:
                    return datetime.strptime(s, fmt).replace(tzinfo=UTC)
                except ValueError:
                    continue
            return None

    gaps = []
    for i in range(len(boots) - 1):
        end_ts = parse_timestamp(boots[i]["end_str"])
        start_ts = parse_timestamp(boots[i + 1]["start_str"])

        if end_ts is None or start_ts is None:
            continue

        gap = start_ts - end_ts
        if gap > gap_threshold:
            gaps.append(
                JournalGap(
                    last_entry=end_ts,
                    next_boot=start_ts,
                    gap=gap,
                )
            )

    return gaps


# ---------------------------------------------------------------------------
# MCP surface drift (wraps the spec-§10 drift detector)
# ---------------------------------------------------------------------------


def check_mcp_surface_drift_finding() -> Finding | None:
    """TIDY wrapper around the spec §10 MCP surface drift detector.

    Returns a node-health Finding when
    ``axiom.extensions.builtins.mcp.drift`` reports stale cache vs. live
    extensions; ``None`` when in sync OR when the MCP module isn't
    importable on this node (defensive — the drift check should never
    break audit_node).
    """
    try:
        from axiom.extensions.builtins.mcp.drift import (
            check_mcp_surface_drift,
        )
    except Exception as exc:  # noqa: BLE001 — MCP optional in audit
        log = __import__("logging").getLogger(__name__)
        log.debug("mcp.drift module unavailable: %s", exc)
        return None

    import os
    from pathlib import Path as _Path

    home_env = os.environ.get("AXIOM_HOME")
    if home_env:
        node_root = _Path(home_env)
    else:
        node_root = _Path(os.environ.get("HOME", ".")).expanduser() / ".axiom"

    try:
        finding = check_mcp_surface_drift(node_root=node_root)
    except Exception as exc:  # noqa: BLE001
        log = __import__("logging").getLogger(__name__)
        log.warning("mcp drift check raised: %s", exc)
        return None
    if finding is None:
        return None
    return Finding(
        check="mcp.surface.stale",
        severity=Severity.INFO,
        message=(
            "MCP surface cache is stale; run `axi mcp regenerate` to "
            "refresh (the TIDY drift proposer will offer this after the "
            "next heartbeat)."
        ),
        auto_fixable=True,
    )


_APT_SIGNED_BY_RE = re.compile(r"signed-by=([^\]\s]+)")


def check_apt_keyrings(run=_run) -> list[Finding]:
    """Detect apt repositories whose signing keyring is missing or empty.

    The classic footgun: a repo is added with
    ``deb [signed-by=/etc/apt/keyrings/foo.gpg] ...`` but the
    ``curl ... | gpg --dearmor -o`` step that should populate the keyring
    failed (offline, proxy, typo), leaving a 0-byte file. Because the file
    now *exists*, re-running an installer's ``gpg --dearmor`` (without
    ``--yes``) silently refuses to overwrite it, so every ``apt update``
    thereafter warns ``NO_PUBKEY`` / "repository is not signed" — forever,
    and quietly, since ``apt update`` still exits 0.

    Observed on a self-hosted node 2026-06-22: the kubernetes v1.32 repo pointed at an
    empty ``kubernetes-apt-keyring.gpg`` (NO_PUBKEY 234654DA9A296436).

    Reported as WARNING and not auto-fixed: the correct key is repo-specific
    (we can't know the upstream key URL generically), so the remedy is
    surfaced to the operator. TRIAGE's ``apt-keyring-missing`` diagnosis
    carries the concrete re-fetch recipe.
    """
    if run is _run and sys.platform != "linux":
        return []

    rc, out = run(
        [
            "grep",
            "-rhoE",
            r"signed-by=[^]\s]+",
            "/etc/apt/sources.list",
            "/etc/apt/sources.list.d",
        ]
    )
    if rc != 0 or not out:
        return []

    findings: list[Finding] = []
    seen: set[str] = set()
    for line in out.splitlines():
        m = _APT_SIGNED_BY_RE.search(line)
        if not m:
            continue
        path = m.group(1)
        if path in seen:
            continue
        seen.add(path)

        rc_stat, size = run(["stat", "-c", "%s", path])
        if rc_stat != 0:
            findings.append(
                Finding(
                    check="apt_keyring",
                    severity=Severity.WARNING,
                    message=(
                        f"apt repo references signing keyring {path} but the file "
                        "is missing — `apt update` cannot verify that repository "
                        "(NO_PUBKEY)"
                    ),
                    current_value="missing",
                    expected_value="present, non-empty keyring",
                    auto_fixable=False,
                )
            )
        elif size.strip() == "0":
            findings.append(
                Finding(
                    check="apt_keyring",
                    severity=Severity.WARNING,
                    message=(
                        f"apt signing keyring {path} is empty (0 bytes) — the key "
                        "download during install failed, so `apt update` reports "
                        "the repository as not signed (NO_PUBKEY)"
                    ),
                    current_value="0 bytes",
                    expected_value="non-empty keyring",
                    auto_fixable=False,
                )
            )
    return findings


# ---------------------------------------------------------------------------
# Aggregate audit
# ---------------------------------------------------------------------------


def audit_node(
    run=_run,
    boot_list_output: str = "",
    version_checker=None,
    directive_store=None,
) -> NodeHealthReport:
    """Run all node health probes and return an aggregate report.

    ``version_checker`` / ``directive_store`` are injectable so tests can
    stay hermetic — left as ``None`` (the default), ``check_stale_version``
    hits the real network/install version, which makes ambient state leak
    into the result.
    """
    report = NodeHealthReport()

    # Misconfiguration checks
    checks = [
        check_gnome_suspend(run=run),
        check_cpu_governor(run=run),
        check_max_cstate(run=run),
        check_kdump(run=run),
        check_rasdaemon(run=run),
        check_desktop_environment(run=run),
        check_home_dir_ownership(run=run),
        check_authorized_keys(run=run),
    ]
    for finding in checks:
        if finding is not None:
            report.findings.append(finding)

    # Sleep targets return a list
    report.findings.extend(check_sleep_targets(run=run))

    # Infrastructure health
    report.findings.extend(check_agent_services(run=run))
    report.findings.extend(check_legacy_cron(run=run))
    report.findings.extend(check_port_conflicts(run=run))
    report.findings.extend(check_service_installations(run=run))
    report.findings.extend(check_apt_keyrings(run=run))
    report.findings.extend(
        check_stale_version(
            version_checker=version_checker, directive_store=directive_store
        )
    )

    # Journal gap analysis
    if not boot_list_output:
        rc, boot_list_output = run(["journalctl", "--list-boots", "--no-pager"])
        if rc != 0:
            boot_list_output = ""

    if boot_list_output:
        report.journal_gaps = detect_journal_gaps(boot_list_output)

    return report
