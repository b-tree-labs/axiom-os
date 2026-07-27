# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""TIDY unit-inventory audit — supervise the supervisors.

Sister skill to ``heartbeat_liveness``. Where that one catches "the
agent went silent," this one catches the layer above: "the agent
supervisor was misconfigured."

Three failure modes this catches, all observed in the wild:

1. **Duplicate registrations.** Two launchd plists (``com.axi-platform``
   AND ``com.axiom-os``) racing on the same state file, crash-looping
   each other 19,000+ times over a week (2026-06-01 autopsy).
2. **Stale references.** A unit's ``ProgramArguments`` / ``ExecStart``
   points at a binary that no longer exists after a venv recreate or
   package rename. systemd's ``status=203/EXEC`` crash-loop (2026-05-22
   self-hosted node incident).
3. **Stale-loaded units.** The plist file is gone from disk but the
   registration lingers in ``launchctl list`` until reboot.

Cross-platform: macOS launchd today; systemd Linux next; Windows
Service later. The data shape is provider-agnostic.
"""

from __future__ import annotations

import logging
import subprocess
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

log = logging.getLogger("axiom.hygiene.unit_inventory")


# ---------------------------------------------------------------------------
# Data shapes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ManagedUnit:
    """A unit that's either registered with the OS supervisor, exists on
    disk as a plist/service file, or both. Provider-agnostic."""

    label: str
    """The unit's identifier (launchd label, systemd unit name)."""
    provider: str
    """``launchd`` / ``systemd`` / ``windows-service``."""
    plist_path: Path | None
    """The on-disk source file, if it exists."""
    is_loaded: bool
    """True if the OS supervisor currently has this unit registered."""
    last_exit_code: int | None
    """The unit's last exit code per ``launchctl list`` etc."""
    program_path: Path | None
    """The executable the unit invokes (from ProgramArguments / ExecStart)."""


@dataclass(frozen=True)
class UnitFinding:
    """One hygiene finding emitted to TIDY."""

    label: str
    severity: str
    """``duplicate`` / ``missing_program`` / ``stale_loaded`` / ``crash_loop``."""
    detail: str
    units_involved: tuple[str, ...]
    """For duplicate findings, the labels of the racing units."""


# ---------------------------------------------------------------------------
# launchd backend
# ---------------------------------------------------------------------------


def _user_launch_agents_dir() -> Path:
    return Path.home() / "Library" / "LaunchAgents"


def _parse_plist_program(path: Path) -> Path | None:
    """Extract the first ProgramArguments path from a plist."""
    try:
        content = path.read_text()
    except OSError:
        return None
    # Cheap targeted parse — full plistlib for robustness.
    try:
        import plistlib

        with path.open("rb") as f:
            data = plistlib.load(f)
        args = data.get("ProgramArguments")
        if args and isinstance(args, list) and args:
            return Path(args[0])
        program = data.get("Program")
        if program:
            return Path(program)
    except Exception:
        # Targeted fallback for malformed plists.
        for line in content.splitlines():
            line = line.strip()
            if line.startswith("<string>/") and (
                "ProgramArguments" in content or "Program" in content
            ):
                return Path(line.removeprefix("<string>").removesuffix("</string>"))
    return None


def list_launchctl_loaded(
    *, runner: Callable = subprocess.run
) -> dict[str, tuple[int | None, int | None]]:
    """Return ``{label: (pid, exit_code)}`` from ``launchctl list``."""
    try:
        r = runner(["launchctl", "list"], capture_output=True, text=True, check=False)
    except FileNotFoundError:
        return {}
    if r.returncode != 0:
        return {}
    out: dict[str, tuple[int | None, int | None]] = {}
    for line in r.stdout.splitlines()[1:]:
        parts = line.split("\t")
        if len(parts) < 3:
            continue
        pid_s, code_s, label = parts[0].strip(), parts[1].strip(), parts[2].strip()
        pid = None if pid_s == "-" else (int(pid_s) if pid_s.isdigit() else None)
        code = None if code_s == "-" else (int(code_s) if code_s.lstrip("-").isdigit() else None)
        out[label] = (pid, code)
    return out


def _label_prefix_for_brand_filter() -> tuple[str, ...]:
    """Labels we own (so we ignore Apple / Homebrew / unrelated entries).

    Platform-owned prefixes plus one derived from each installed portfolio
    member's package name, so a domain consumer's units are swept without the
    platform hardcoding any consumer brand.
    """
    prefixes = ["com.axiom", "com.axi-"]
    try:
        from axiom.infra.branding import discover_portfolio_members

        for member in discover_portfolio_members():
            pkg = getattr(member, "package_name", "") or ""
            if pkg and pkg != "axiom-os-lm":
                brand = pkg.removesuffix("-lm")
                prefixes += [f"com.{brand}", f"com.{brand.replace('-', '_')}"]
    except Exception:
        pass
    return tuple(dict.fromkeys(prefixes))


def discover_launchd_units(
    *, runner: Callable = subprocess.run
) -> list[ManagedUnit]:
    """Enumerate axiom-portfolio launchd units on this host."""
    units: list[ManagedUnit] = []
    loaded = list_launchctl_loaded(runner=runner)
    brand_prefixes = _label_prefix_for_brand_filter()

    # 1. Units that exist on disk.
    agents_dir = _user_launch_agents_dir()
    disk_labels: set[str] = set()
    if agents_dir.exists():
        for plist in agents_dir.glob("com.*.plist"):
            label = plist.stem  # strips ".plist"
            if not label.startswith(brand_prefixes):
                continue
            disk_labels.add(label)
            pid, code = loaded.get(label, (None, None))
            units.append(
                ManagedUnit(
                    label=label,
                    provider="launchd",
                    plist_path=plist,
                    is_loaded=label in loaded,
                    last_exit_code=code,
                    program_path=_parse_plist_program(plist),
                )
            )

    # 2. Units in launchctl that don't exist on disk (stale-loaded).
    for label, (pid, code) in loaded.items():
        if not label.startswith(brand_prefixes):
            continue
        if label in disk_labels:
            continue
        units.append(
            ManagedUnit(
                label=label,
                provider="launchd",
                plist_path=None,
                is_loaded=True,
                last_exit_code=code,
                program_path=None,
            )
        )

    return units


# ---------------------------------------------------------------------------
# Cross-provider audit + findings
# ---------------------------------------------------------------------------


def _ours_invokes_same_program(units: list[ManagedUnit]) -> list[list[ManagedUnit]]:
    """Group units that share the same ``program_path``."""
    by_program: dict[Path, list[ManagedUnit]] = defaultdict(list)
    for u in units:
        if u.program_path is None:
            continue
        by_program[u.program_path].append(u)
    return [v for v in by_program.values() if len(v) > 1]


def audit_units(
    units: list[ManagedUnit] | None = None,
    *,
    runner: Callable = subprocess.run,
) -> list[UnitFinding]:
    """Build hygiene findings from a unit inventory."""
    if units is None:
        units = discover_launchd_units(runner=runner)
    findings: list[UnitFinding] = []

    # Pattern 1: duplicate registrations (two units, same program).
    for group in _ours_invokes_same_program(units):
        labels = tuple(sorted(u.label for u in group))
        findings.append(
            UnitFinding(
                label=labels[0],
                severity="duplicate",
                detail=(
                    f"{len(group)} units invoke the same program "
                    f"{group[0].program_path}: {', '.join(labels)} — "
                    "racing supervisors will clobber each other's state. "
                    "Run `axi agents register` to clean stale units."
                ),
                units_involved=labels,
            )
        )

    # Pattern 2: program path doesn't exist.
    for u in units:
        if u.program_path is None:
            continue
        if not u.program_path.exists():
            findings.append(
                UnitFinding(
                    label=u.label,
                    severity="missing_program",
                    detail=(
                        f"unit {u.label!r} ProgramArguments points at "
                        f"{u.program_path} which does not exist on disk; "
                        "likely a venv recreate or package rename"
                    ),
                    units_involved=(u.label,),
                )
            )

    # Pattern 3: stale-loaded (registered with launchctl, no plist on disk).
    for u in units:
        if u.is_loaded and u.plist_path is None:
            findings.append(
                UnitFinding(
                    label=u.label,
                    severity="stale_loaded",
                    detail=(
                        f"unit {u.label!r} is registered with launchctl but "
                        "has no plist on disk — run "
                        f"`launchctl bootout gui/$(id -u)/{u.label}`"
                    ),
                    units_involved=(u.label,),
                )
            )

    # Pattern 4: crash-looping (negative exit codes or repeated non-zero on
    # short-lived oneshots). Heuristic for now; richer signal once we
    # persist per-unit history.
    for u in units:
        if u.last_exit_code is not None and u.last_exit_code != 0:
            findings.append(
                UnitFinding(
                    label=u.label,
                    severity="crash_loop",
                    detail=(
                        f"unit {u.label!r} last exited with code "
                        f"{u.last_exit_code}; check stderr log"
                    ),
                    units_involved=(u.label,),
                )
            )

    return findings


__all__ = [
    "ManagedUnit",
    "UnitFinding",
    "audit_units",
    "discover_launchd_units",
    "list_launchctl_loaded",
]
