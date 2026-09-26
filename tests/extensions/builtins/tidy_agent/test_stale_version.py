# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tests for check_stale_version — Tidy's version drift health check."""

from __future__ import annotations

from unittest.mock import MagicMock

from axiom.extensions.builtins.hygiene.node_health import (
    Severity,
    check_stale_version,
)
from axiom.policy.version_directive_store import VersionDirective


class _StubStore:
    """Injectable directive store for testing."""

    def __init__(self, directives):
        self._directives = directives

    def load_active(self):
        return self._directives


def _checker(current, available, is_newer=True):
    checker = MagicMock()
    checker.check_remote_version.return_value = MagicMock(
        current=current, available=available, is_newer=is_newer
    )
    checker.get_current_version.return_value = current
    return checker


def test_no_drift_no_findings():
    checker = _checker("0.9.0", "0.9.0", is_newer=False)
    store = _StubStore([])
    findings = check_stale_version(version_checker=checker, directive_store=store)
    assert findings == []


def test_patch_drift_is_info_severity():
    """0.9.0 → 0.9.1 is a patch bump — info-level nudge, not a warning."""
    checker = _checker("0.9.0", "0.9.1")
    store = _StubStore([])
    findings = check_stale_version(version_checker=checker, directive_store=store)
    assert len(findings) == 1
    assert findings[0].check == "stale_version"
    assert findings[0].severity == Severity.INFO
    assert "0.9.0 → 0.9.1" in findings[0].message


def test_minor_drift_is_warning():
    """0.9.0 → 0.10.0 is a minor bump — warning, operator missed a release."""
    checker = _checker("0.9.0", "0.10.0")
    store = _StubStore([])
    findings = check_stale_version(version_checker=checker, directive_store=store)
    assert len(findings) == 1
    assert findings[0].severity == Severity.WARNING


def test_directive_violation_emits_finding():
    """Local 0.8.0 violates a directive requiring >= 0.10.0."""
    checker = _checker("0.8.0", "0.8.0", is_newer=False)
    directive = VersionDirective(
        package="axiom-os-lm",
        min_version="0.10.0",
        issuer="@ben.booth:axiom",
        deadline="",
    )
    store = _StubStore([directive])
    findings = check_stale_version(version_checker=checker, directive_store=store)
    assert len(findings) == 1
    assert findings[0].check == "version_directive_violation"
    assert findings[0].severity == Severity.WARNING
    assert "0.10.0" in findings[0].message
    assert "@ben.booth:axiom" in findings[0].message


def test_directive_satisfied_no_finding():
    """Local 0.11.0 satisfies a directive requiring >= 0.10.0."""
    checker = _checker("0.11.0", "0.11.0", is_newer=False)
    directive = VersionDirective(
        package="axiom-os-lm",
        min_version="0.10.0",
        issuer="@ben.booth:axiom",
    )
    store = _StubStore([directive])
    findings = check_stale_version(version_checker=checker, directive_store=store)
    assert findings == []


def test_network_failure_is_swallowed():
    """A VersionChecker that raises must not fail the whole health report."""
    checker = MagicMock()
    checker.check_remote_version.side_effect = ConnectionError("no network")
    checker.get_current_version.return_value = "0.9.0"
    store = _StubStore([])
    # Should not raise
    findings = check_stale_version(version_checker=checker, directive_store=store)
    assert findings == []
