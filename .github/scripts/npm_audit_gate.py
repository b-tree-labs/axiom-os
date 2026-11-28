#!/usr/bin/env python3
# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Fail an npm audit JSON report when a finding is high or critical.

npm audit reads a lockfile and asks the registry for known advisories. Its
own exit code cannot tell "found something" from "could not look". This
reads the JSON instead. High and critical fail the run. Anything below
high is printed and does not fail it. A report npm could not produce, or
one in a shape this does not know, fails the run, because that lockfile
was not checked.

Usage: npm_audit_gate.py report.json [report.json ...]
"""

from __future__ import annotations

import json
import sys

_FAILING = ("high", "critical")
_KNOWN = ("info", "low", "moderate", "high", "critical")


class Unreadable(Exception):
    """The report does not say what the audit found."""


def findings(report: dict) -> list[tuple[str, str, str]]:
    """(package, severity, what) for every finding. Raises Unreadable if unsure."""
    if "error" in report:
        error = report["error"] or {}
        raise Unreadable(f"npm audit failed: {error.get('code')} {error.get('summary')}")
    if report.get("auditReportVersion") != 2:
        raise Unreadable(f"unknown report version: {report.get('auditReportVersion')!r}")
    vulnerabilities = report.get("vulnerabilities")
    counts = (report.get("metadata") or {}).get("vulnerabilities")
    if not isinstance(vulnerabilities, dict) or not isinstance(counts, dict):
        raise Unreadable("report has no findings section")
    found = []
    for name, entry in sorted(vulnerabilities.items()):
        severity = str(entry.get("severity", "")).lower()
        if severity not in _KNOWN:
            raise Unreadable(f"{name}: unknown severity {severity!r}")
        titles = [
            via["title"]
            for via in entry.get("via", [])
            if isinstance(via, dict) and via.get("title")
        ]
        what = "; ".join(titles) or "through " + ", ".join(str(via) for via in entry.get("via", []))
        found.append((name, severity, f"{entry.get('range', '')} {what}".strip()))
    if len(found) != counts.get("total"):
        raise Unreadable(f"report lists {len(found)} findings and counts {counts.get('total')}")
    return found


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args:
        print("usage: npm_audit_gate.py report.json [report.json ...]", file=sys.stderr)
        return 2
    failing = 0
    unread = 0
    for path in args:
        try:
            with open(path, encoding="utf-8") as handle:
                found = findings(json.load(handle))
        except (OSError, json.JSONDecodeError, Unreadable) as exc:
            print(f"{path}: not checked: {exc}", file=sys.stderr)
            unread += 1
            continue
        print(f"{path}: {len(found)} finding(s)")
        for name, severity, what in found:
            print(f"  {severity.upper():8} {name} {what}")
            failing += severity in _FAILING
    print(f"{failing} high or critical finding(s), {unread} report(s) not checked")
    return 1 if failing or unread else 0


if __name__ == "__main__":
    sys.exit(main())
