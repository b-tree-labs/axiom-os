#!/usr/bin/env python3
# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Fail a pip-audit JSON report when a finding is high or critical.

pip-audit lists known vulnerabilities and exits if any exist. It does not
carry a severity. This reads that JSON, asks OSV for each advisory, and
scores CVSS v3. High is a base score of 7.0 or more. Critical is 9.0 or
more. Anything below high is printed and does not fail the run. A
dependency pip-audit had to skip fails the run, because that package was
not checked.

Usage: pip_audit_gate.py report.json [report.json ...]
"""

from __future__ import annotations

import json
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from decimal import ROUND_CEILING, Decimal

# CVSS v3.0 / v3.1 base metrics. Spec: https://www.first.org/cvss/v3-1/
_AV = {"N": Decimal("0.85"), "A": Decimal("0.62"), "L": Decimal("0.55"), "P": Decimal("0.2")}
_AC = {"L": Decimal("0.77"), "H": Decimal("0.44")}
_PR_UNCHANGED = {"N": Decimal("0.85"), "L": Decimal("0.62"), "H": Decimal("0.27")}
_PR_CHANGED = {"N": Decimal("0.85"), "L": Decimal("0.68"), "H": Decimal("0.5")}
_UI = {"N": Decimal("0.85"), "R": Decimal("0.62")}
_CIA = {"N": Decimal("0"), "L": Decimal("0.22"), "H": Decimal("0.56")}
_BASE_KEYS = ("AV", "AC", "PR", "UI", "S", "C", "I", "A")
_RANK = {"UNKNOWN": -1, "NONE": 0, "LOW": 1, "MEDIUM": 2, "HIGH": 3, "CRITICAL": 4}


def _round_up(value: Decimal) -> Decimal:
    """Smallest one-decimal number greater than or equal to value."""
    return value.quantize(Decimal("0.1"), rounding=ROUND_CEILING)


def cvss_v3_base(vector: str) -> Decimal | None:
    """Base score for a CVSS v3.0 or v3.1 vector. None if it is not one."""
    parts = (vector or "").split("/")
    if not parts or not parts[0].startswith("CVSS:3."):
        return None
    metrics: dict[str, str] = {}
    for part in parts[1:]:
        if ":" not in part:
            continue
        key, value = part.split(":", 1)
        metrics[key] = value
    if any(key not in metrics for key in _BASE_KEYS):
        return None
    try:
        av = _AV[metrics["AV"]]
        ac = _AC[metrics["AC"]]
        ui = _UI[metrics["UI"]]
        c = _CIA[metrics["C"]]
        i = _CIA[metrics["I"]]
        a = _CIA[metrics["A"]]
        scope = metrics["S"]
        pr = (_PR_CHANGED if scope == "C" else _PR_UNCHANGED)[metrics["PR"]]
    except KeyError:
        return None
    if scope not in ("U", "C"):
        return None
    impact = Decimal("1") - (Decimal("1") - c) * (Decimal("1") - i) * (Decimal("1") - a)
    if scope == "U":
        impact_score = Decimal("6.42") * impact
    else:
        impact_score = Decimal("7.52") * (impact - Decimal("0.029")) - Decimal("3.25") * (
            (impact - Decimal("0.02")) ** 15
        )
    exploitability = Decimal("8.22") * av * ac * pr * ui
    if impact_score <= 0:
        return Decimal("0.0")
    if scope == "U":
        raw = impact_score + exploitability
    else:
        raw = Decimal("1.08") * (impact_score + exploitability)
    if raw > 10:
        raw = Decimal("10")
    return _round_up(raw)


def severity_label(score: Decimal) -> str:
    if score <= 0:
        return "NONE"
    if score < Decimal("4.0"):
        return "LOW"
    if score < Decimal("7.0"):
        return "MEDIUM"
    if score < Decimal("9.0"):
        return "HIGH"
    return "CRITICAL"


def label_for_advisory(document: dict | None) -> str:
    """Worst label on an OSV advisory. UNKNOWN when nothing here is scored."""
    if not document:
        return "UNKNOWN"
    labels: list[str] = []
    for item in document.get("severity") or []:
        score = str(item.get("score") or "")
        kind = str(item.get("type") or "")
        if kind.upper().startswith("CVSS_V3") or score.startswith("CVSS:3"):
            base = cvss_v3_base(score)
            if base is not None:
                labels.append(severity_label(base))
    raw = (document.get("database_specific") or {}).get("severity")
    if isinstance(raw, str) and raw.upper() in _RANK and raw.upper() != "UNKNOWN":
        labels.append(raw.upper())
    if not labels:
        return "UNKNOWN"
    return max(labels, key=lambda label: _RANK[label])


def fails(label: str) -> bool:
    return label in ("HIGH", "CRITICAL")


@dataclass(frozen=True)
class Row:
    name: str
    version: str
    vuln_id: str
    severity: str


def collect(report: dict, lookup) -> tuple[list[Row], list[str]]:
    rows: list[Row] = []
    skipped: list[str] = []
    for dep in report.get("dependencies") or []:
        name = str(dep.get("name") or "?")
        if dep.get("skip_reason"):
            skipped.append(f"{name}: {dep['skip_reason']}")
            continue
        version = str(dep.get("version") or "")
        for vuln in dep.get("vulns") or []:
            ids = [vuln.get("id"), *(vuln.get("aliases") or [])]
            ids = [str(item) for item in ids if item]
            document = None
            for vuln_id in ids:
                document = lookup(vuln_id)
                if document:
                    break
            rows.append(Row(name, version, ids[0] if ids else "?", label_for_advisory(document)))
    return rows, skipped


def render(rows: list[Row], skipped: list[str]) -> str:
    lines = []
    if not rows and not skipped:
        lines.append("No known vulnerabilities.")
    for row in sorted(rows, key=lambda item: (item.name, item.vuln_id)):
        lines.append(f"{row.severity}\t{row.name} {row.version}\t{row.vuln_id}")
    for reason in skipped:
        lines.append(f"SKIP\t{reason}")
    failing = [row for row in rows if fails(row.severity)]
    lines.append(
        f"{len(failing)} high or critical, {len(rows) - len(failing)} below high, {len(skipped)} skipped"
    )
    return "\n".join(lines)


def gate(reports: list[dict], lookup) -> int:
    rows: list[Row] = []
    skipped: list[str] = []
    for report in reports:
        more_rows, more_skipped = collect(report, lookup)
        rows.extend(more_rows)
        skipped.extend(more_skipped)
    print(render(rows, skipped))
    if skipped or any(fails(row.severity) for row in rows):
        return 1
    return 0


def fetch_osv(vuln_id: str) -> dict | None:
    quoted = urllib.parse.quote(vuln_id, safe="")
    request = urllib.request.Request(
        f"https://api.osv.dev/v1/vulns/{quoted}",
        headers={"Accept": "application/json", "User-Agent": "axiom-pip-audit-gate"},
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return None
        raise


def load_reports(paths: list[str]) -> list[dict]:
    reports = []
    for path in paths:
        with open(path, encoding="utf-8") as handle:
            reports.append(json.load(handle))
    return reports


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args:
        print("usage: pip_audit_gate.py report.json [report.json ...]", file=sys.stderr)
        return 2
    try:
        reports = load_reports(args)
    except (OSError, json.JSONDecodeError) as exc:
        print(f"could not read pip-audit report: {exc}", file=sys.stderr)
        return 1
    try:
        return gate(reports, fetch_osv)
    except urllib.error.URLError as exc:
        print(f"could not read OSV: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
