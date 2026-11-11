# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""pip-audit fails the build only at high severity and above."""

from __future__ import annotations

import sys
from decimal import Decimal
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parents[1] / ".github" / "scripts"
sys.path.insert(0, str(_SCRIPTS))

import pip_audit_gate as gate  # noqa: E402


def test_cvss_v3_scores_match_the_spec():
    expect = {
        "CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:H": (Decimal("9.8"), "CRITICAL"),
        "CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:N/A:N": (Decimal("7.5"), "HIGH"),
        "CVSS:3.1/AV:N/AC:L/PR:N/UI:R/S:C/C:L/I:L/A:N": (Decimal("6.1"), "MEDIUM"),
        "CVSS:3.1/AV:N/AC:H/PR:N/UI:N/S:U/C:N/I:N/A:H": (Decimal("5.9"), "MEDIUM"),
        "CVSS:3.0/AV:N/AC:L/PR:L/UI:N/S:U/C:H/I:N/A:N": (Decimal("6.5"), "MEDIUM"),
        "CVSS:3.1/AV:L/AC:H/PR:H/UI:R/S:U/C:L/I:N/A:N": (Decimal("1.8"), "LOW"),
        "CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:C/C:H/I:H/A:H": (Decimal("10.0"), "CRITICAL"),
    }
    for vector, (score, label) in expect.items():
        assert gate.cvss_v3_base(vector) == score
        assert gate.severity_label(score) == label
    assert gate.cvss_v3_base("CVSS:4.0/AV:N/AC:L/AT:N/PR:N/UI:N/VC:H/VI:H/VA:H/SC:N/SI:N/SA:N") is None


def test_high_and_critical_fail_and_lower_do_not():
    assert gate.fails("HIGH") and gate.fails("CRITICAL")
    assert not gate.fails("MEDIUM")
    assert not gate.fails("LOW")
    assert not gate.fails("UNKNOWN")


def _report(vulns, skip=None):
    dep = {"name": "example", "version": "1.0.0", "vulns": vulns}
    if skip:
        dep = {"name": "example", "skip_reason": skip}
    return {"dependencies": [dep]}


def test_gate_fails_on_high_and_passes_on_medium(capsys):
    high = {
        "severity": [{"type": "CVSS_V3", "score": "CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:N/A:N"}]
    }
    medium = {
        "severity": [{"type": "CVSS_V3", "score": "CVSS:3.1/AV:N/AC:L/PR:N/UI:R/S:C/C:L/I:L/A:N"}]
    }
    looked = []

    def lookup(vuln_id):
        looked.append(vuln_id)
        return high if vuln_id == "PYSEC-1" else None

    report = _report([{"id": "PYSEC-1", "aliases": ["CVE-1"]}])
    assert gate.gate([report], lookup) == 1
    assert looked == ["PYSEC-1"]
    assert "HIGH" in capsys.readouterr().out

    def lookup_medium(vuln_id):
        return medium

    assert gate.gate([_report([{"id": "PYSEC-2"}])], lookup_medium) == 0


def test_alias_is_used_when_the_primary_id_is_missing():
    high = {
        "database_specific": {"severity": "HIGH"},
    }

    def lookup(vuln_id):
        if vuln_id == "GHSA-1":
            return high
        return None

    report = _report([{"id": "PYSEC-missing", "aliases": ["GHSA-1"]}])
    assert gate.gate([report], lookup) == 1


def test_skipped_dependency_fails_even_with_no_advisory():
    assert gate.gate([_report([], skip="could not resolve")], lambda _id: None) == 1


def test_clean_report_passes():
    assert gate.gate([{"dependencies": [{"name": "ok", "version": "1", "vulns": []}]}], lambda _id: None) == 0
