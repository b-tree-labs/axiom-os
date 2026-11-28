# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""npm audit fails the build only at high severity and above, and never passes unread."""

from __future__ import annotations

import json
import sys
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parents[1] / ".github" / "scripts"
sys.path.insert(0, str(_SCRIPTS))

import npm_audit_gate as gate  # noqa: E402


def _report(**found: str) -> dict:
    """An npm audit report (version 2) with one finding per package=severity."""
    counts = dict.fromkeys(("info", "low", "moderate", "high", "critical"), 0)
    vulnerabilities = {}
    for name, severity in found.items():
        counts[severity] += 1
        vulnerabilities[name] = {
            "name": name,
            "severity": severity,
            "range": "<1.2.3",
            "via": [{"title": f"{name} is broken", "url": "https://example.invalid/a"}],
            "fixAvailable": True,
        }
    counts["total"] = len(found)
    return {
        "auditReportVersion": 2,
        "vulnerabilities": vulnerabilities,
        "metadata": {"vulnerabilities": counts},
    }


def _write(tmp_path: Path, name: str, report: dict) -> str:
    path = tmp_path / name
    path.write_text(json.dumps(report), encoding="utf-8")
    return str(path)


def test_a_clean_report_passes(tmp_path, capsys):
    assert gate.main([_write(tmp_path, "a.json", _report())]) == 0
    assert "0 high or critical" in capsys.readouterr().out


def test_high_and_critical_fail(tmp_path, capsys):
    for severity in ("high", "critical"):
        path = _write(tmp_path, "a.json", _report(router=severity))
        assert gate.main([path]) == 1
        out = capsys.readouterr().out
        assert "router" in out and severity.upper() in out


def test_lower_findings_are_printed_and_pass(tmp_path, capsys):
    path = _write(tmp_path, "a.json", _report(cookie="moderate", left="low"))
    assert gate.main([path]) == 0
    out = capsys.readouterr().out
    assert "cookie" in out and "MODERATE" in out and "left" in out


def test_one_bad_report_among_several_fails(tmp_path):
    clean = _write(tmp_path, "a.json", _report())
    bad = _write(tmp_path, "b.json", _report(router="high"))
    assert gate.main([clean, bad]) == 1


def test_a_report_npm_could_not_produce_fails(tmp_path, capsys):
    # npm writes {"error": {...}} when it cannot reach the registry or read the lockfile.
    path = _write(tmp_path, "a.json", {"error": {"code": "ENOLOCK", "summary": "no lockfile"}})
    assert gate.main([path]) == 1
    assert "ENOLOCK" in capsys.readouterr().err


def test_an_unknown_report_shape_fails(tmp_path):
    assert gate.main([_write(tmp_path, "a.json", {"auditReportVersion": 2})]) == 1
    assert gate.main([_write(tmp_path, "b.json", {"advisories": {}})]) == 1


def test_an_unknown_severity_fails(tmp_path):
    report = _report(router="high")
    report["vulnerabilities"]["router"]["severity"] = "severe"
    assert gate.main([_write(tmp_path, "a.json", report)]) == 1


def test_a_missing_or_empty_report_fails(tmp_path):
    empty = tmp_path / "empty.json"
    empty.write_text("", encoding="utf-8")
    assert gate.main([str(empty)]) == 1
    assert gate.main([str(tmp_path / "absent.json")]) == 1
    assert gate.main([]) == 2
