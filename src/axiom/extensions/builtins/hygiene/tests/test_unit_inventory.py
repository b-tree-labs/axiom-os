# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tests for TIDY's unit-inventory audit."""

from __future__ import annotations

from pathlib import Path
from unittest import mock


from axiom.extensions.builtins.hygiene.unit_inventory import (
    ManagedUnit,
    audit_units,
    discover_launchd_units,
    list_launchctl_loaded,
)


def _u(
    label: str,
    *,
    program: Path | None = None,
    plist: Path | None = None,
    loaded: bool = True,
    exit_code: int | None = 0,
) -> ManagedUnit:
    return ManagedUnit(
        label=label,
        provider="launchd",
        plist_path=plist,
        is_loaded=loaded,
        last_exit_code=exit_code,
        program_path=program,
    )


# ---------------------------------------------------------------------------
# list_launchctl_loaded
# ---------------------------------------------------------------------------


class TestLaunchctlList:
    def test_parses_lines(self):
        def fake(*args, **kwargs):
            return mock.Mock(
                returncode=0,
                stdout=(
                    "PID\tStatus\tLabel\n"
                    "1234\t0\tcom.axiom-os.background-service\n"
                    "-\t3\tcom.axi-platform.background-service\n"
                    "5678\t0\tcom.axiom.ollama\n"
                ),
            )

        out = list_launchctl_loaded(runner=fake)
        assert out == {
            "com.axiom-os.background-service": (1234, 0),
            "com.axi-platform.background-service": (None, 3),
            "com.axiom.ollama": (5678, 0),
        }

    def test_missing_binary_returns_empty(self):
        def fake(*args, **kwargs):
            raise FileNotFoundError("no launchctl")

        assert list_launchctl_loaded(runner=fake) == {}


# ---------------------------------------------------------------------------
# audit_units — the four patterns
# ---------------------------------------------------------------------------


class TestAuditFindings:
    def test_duplicate_supervisors_caught(self):
        program = Path("/venv/bin/Axiom-Background-Service")
        units = [
            _u("com.axiom-os.background-service", program=program),
            _u("com.axi-platform.background-service", program=program),
        ]
        findings = audit_units(units)
        dups = [f for f in findings if f.severity == "duplicate"]
        assert len(dups) == 1
        assert set(dups[0].units_involved) == {
            "com.axiom-os.background-service",
            "com.axi-platform.background-service",
        }

    def test_missing_program_caught(self, tmp_path):
        # Use a path that's guaranteed not to exist.
        missing = tmp_path / "ghost" / "nope"
        units = [_u("com.test.svc", program=missing)]
        findings = audit_units(units)
        miss = [f for f in findings if f.severity == "missing_program"]
        assert len(miss) == 1
        assert miss[0].label == "com.test.svc"

    def test_present_program_not_flagged(self, tmp_path):
        real = tmp_path / "real"
        real.write_text("")
        units = [_u("com.test.svc", program=real)]
        findings = audit_units(units)
        assert not any(f.severity == "missing_program" for f in findings)

    def test_stale_loaded_caught(self):
        # Loaded in launchctl, no plist on disk.
        units = [_u("com.zombie.svc", plist=None, loaded=True)]
        findings = audit_units(units)
        stale = [f for f in findings if f.severity == "stale_loaded"]
        assert len(stale) == 1
        assert stale[0].label == "com.zombie.svc"

    def test_nonzero_exit_caught(self):
        units = [_u("com.axiom-os.bad", exit_code=3)]
        findings = audit_units(units)
        loops = [f for f in findings if f.severity == "crash_loop"]
        assert len(loops) == 1

    def test_zero_exit_not_flagged(self):
        units = [_u("com.axiom-os.ok", exit_code=0)]
        findings = audit_units(units)
        assert not any(f.severity == "crash_loop" for f in findings)

    def test_unloaded_with_plist_is_quiet(self, tmp_path):
        # Unloaded + has a plist file → no finding (normal "not running" state).
        plist = tmp_path / "x.plist"
        plist.write_text("")
        units = [_u("com.test.idle", plist=plist, loaded=False, exit_code=0)]
        findings = audit_units(units)
        assert findings == []


# ---------------------------------------------------------------------------
# Discovery — integration with the test fs
# ---------------------------------------------------------------------------


class TestDiscoverLaunchdUnits:
    def test_discovers_plist_on_disk(self, tmp_path, monkeypatch):
        agents_dir = tmp_path / "Library" / "LaunchAgents"
        agents_dir.mkdir(parents=True)
        plist = agents_dir / "com.axiom-os.background-service.plist"
        plist.write_text(
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<plist version="1.0">\n<dict>\n'
            '<key>Label</key><string>com.axiom-os.background-service</string>\n'
            '<key>ProgramArguments</key><array>\n'
            '<string>/venv/bin/Axiom-Background-Service</string>\n'
            '</array>\n</dict>\n</plist>\n'
        )

        def fake_home():
            return tmp_path

        monkeypatch.setattr(Path, "home", staticmethod(fake_home))

        def fake_runner(*args, **kwargs):
            return mock.Mock(returncode=1, stdout="")  # nothing loaded

        units = discover_launchd_units(runner=fake_runner)
        assert len(units) == 1
        u = units[0]
        assert u.label == "com.axiom-os.background-service"
        assert u.program_path == Path("/venv/bin/Axiom-Background-Service")
        assert u.is_loaded is False  # nothing loaded in launchctl

    def test_discovers_stale_loaded(self, tmp_path, monkeypatch):
        # Nothing on disk but launchctl reports it.
        empty_dir = tmp_path / "Library" / "LaunchAgents"
        empty_dir.mkdir(parents=True)

        monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))

        def fake_runner(*args, **kwargs):
            return mock.Mock(
                returncode=0,
                stdout=(
                    "PID\tStatus\tLabel\n"
                    "-\t0\tcom.axi-platform.background-service\n"
                ),
            )

        units = discover_launchd_units(runner=fake_runner)
        assert len(units) == 1
        assert units[0].plist_path is None
        assert units[0].is_loaded is True

    def test_ignores_non_axiom_labels(self, tmp_path, monkeypatch):
        monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))

        def fake_runner(*args, **kwargs):
            return mock.Mock(
                returncode=0,
                stdout=(
                    "PID\tStatus\tLabel\n"
                    "100\t0\tcom.apple.somebusiness\n"
                    "200\t0\tcom.google.somethirdparty\n"
                    "300\t0\tcom.axiom-os.background-service\n"
                ),
            )

        units = discover_launchd_units(runner=fake_runner)
        labels = [u.label for u in units]
        assert labels == ["com.axiom-os.background-service"]


# ---------------------------------------------------------------------------
# Regression — the 2026-06-01 duplicate-supervisor scenario
# ---------------------------------------------------------------------------


class TestDuplicateSupervisorRegression:
    """The exact 19,182-traceback racing-supervisors incident from 2026-06-01."""

    def test_duplicate_pair_surfaced_with_remediation(self):
        bg = Path("/Users/example/Projects/workspace/.venv/bin/Axiom-Background-Service")
        units = [
            _u("com.axiom-os.background-service", program=bg),
            _u("com.axi-platform.background-service", program=bg),
        ]
        findings = audit_units(units)
        dups = [f for f in findings if f.severity == "duplicate"]
        assert len(dups) == 1
        # The remediation hint must name the canonical cleanup command.
        assert "axi agents register" in dups[0].detail
