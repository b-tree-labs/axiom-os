#!/usr/bin/env python3
# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""File or update ONE incident for a failing Semgrep scan on main or the nightly.

Reads the JSON the scan uploaded, names the rule ids and files of its findings, links the
run, and either appends an occurrence to the open Semgrep incident or opens it. It never
reverts, re-runs or closes anything: a Semgrep finding is not a reason to revert. The incident
closes through the stale-incident rule (ci_health.stale_incidents), which reads the marker
below, so the marker, the title and the occurrence table here are a contract with that module.

Only reads the file and calls ``gh``; stdlib only, so the job needs no install.
Env: GH_TOKEN, REPO, RUN_URL, SHA, EVENT_NAME, RESULTS (path to semgrep.json).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime, timezone

KEY = "semgrep"
MARKER = f"<!-- ci-incident:branch={KEY} -->"
TITLE = f"🔴 CI incident on `{KEY}` (Semgrep finding on main)"
LABELS = ("ci-failure", "priority: high")
MAX_LISTED = 25


def read_findings(path: str) -> tuple[list[dict], list[str], bool]:
    """(findings, scan errors, readable). Unreadable results are not an empty scan."""
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
        rows = data["results"]
        assert isinstance(rows, list)
    except (OSError, ValueError, KeyError, AssertionError, TypeError):
        return [], [], False
    seen: set[tuple] = set()
    findings: list[dict] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        extra = row.get("extra") if isinstance(row.get("extra"), dict) else {}
        start = row.get("start") if isinstance(row.get("start"), dict) else {}
        item = {
            "rule": str(row.get("check_id") or "unknown-rule"),
            "path": str(row.get("path") or "unknown-file"),
            "line": int(start.get("line") or 0),
            "severity": str(extra.get("severity") or ""),
        }
        key = (item["rule"], item["path"], item["line"])
        if key not in seen:
            seen.add(key)
            findings.append(item)
    findings.sort(key=lambda f: (f["rule"], f["path"], f["line"]))
    errors = [str(e.get("message") or "")[:200] for e in data.get("errors") or [] if isinstance(e, dict) and e.get("level") == "error"]
    return findings, errors, True


def _code(text: str) -> str:
    return "`" + text.replace("`", "'") + "`"


def occurrence(findings, errors, readable, *, run_url, sha, event, now) -> str:
    kind = "nightly scan" if event == "schedule" else "scan of main"
    lines = [
        f"### New occurrence: {_code(sha[:7])}",
        "",
        "| Field | Value |",
        "|---|---|",
        f"| **Run** | {run_url} |",
        f"| **Commit** | {_code(sha[:7])} |",
        f"| **Scan** | {kind} ({event}) |",
        f"| **At (UTC)** | {now} |",
        "",
    ]
    if not readable:
        lines += ["The scan failed and its results could not be read, so no rule or file is named. Open the run above."]
    elif not findings:
        lines += ["The scan failed with no ERROR finding in its results"
                  + (": " + "; ".join(errors) if errors else ". Open the run above.")]
    else:
        lines += [f"{len(findings)} ERROR finding(s):", ""]
        lines += [f"- {_code(f['rule'])} in {_code(f['path'] + (':' + str(f['line']) if f['line'] else ''))}" for f in findings[:MAX_LISTED]]
        if len(findings) > MAX_LISTED:
            lines += [f"- and {len(findings) - MAX_LISTED} more in the run's results"]
    return "\n".join(lines)


def issue_body(occ: str) -> str:
    return "\n".join([
        MARKER,
        "## Semgrep finding on main",
        "",
        "A Semgrep scan of the whole tree failed on a push to main or on the nightly scan. Findings from",
        "further failing runs are added here as occurrences, so one cause is one incident.",
        "",
        occ,
        "",
        "### Action required",
        "Fix the finding, or suppress it with a reason where it is a false positive. Nothing was reverted:",
        "a Semgrep finding does not revert a commit. This incident closes itself when a later Semgrep run",
        "on main is green.",
        "",
        "---",
        "*Auto-filed by the Semgrep workflow*",
    ])


def gh(*args: str) -> str:
    done = subprocess.run(["gh", *args], capture_output=True, text=True, check=False)
    if done.returncode != 0:
        print(f"gh {args[0]} {args[1]} failed: {done.stderr.strip()}", file=sys.stderr)
        raise SystemExit(1)
    return done.stdout


def main() -> int:
    repo = os.environ["REPO"]
    findings, errors, readable = read_findings(os.environ.get("RESULTS", ""))
    occ = occurrence(
        findings, errors, readable,
        run_url=os.environ["RUN_URL"], sha=os.environ["SHA"], event=os.environ.get("EVENT_NAME", "push"),
        now=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    )
    rows = json.loads(gh("issue", "list", "-R", repo, "--state", "open", "--label", LABELS[0],
                         "--json", "number,body", "--limit", "200") or "[]")
    found = next((r for r in rows if MARKER in str(r.get("body") or "")), None)
    if found:
        gh("issue", "comment", str(found["number"]), "-R", repo, "--body", occ)
        print(f"Appended an occurrence to incident #{found['number']}")
    else:
        args = ["issue", "create", "-R", repo, "--title", TITLE, "--body", issue_body(occ)]
        for label in LABELS:
            args += ["--label", label]
        print(gh(*args).strip())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
