#!/usr/bin/env python3
# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Post or update RIVET's one note on a pull request, and write the run summary.

The note leads with this pull request's verdict: held or not, why, who acts next
and what they do (#2246). It changes nothing. Landing is the workflow's separate
``land --act`` step, which runs from main. This script does not merge, enqueue,
or push. An existing comment with MARKER is edited in place, so a new push
replaces the previous note instead of adding another.

Modes:
  (default)   the note on PR_NUMBER, from the report.
  --refresh   from main: post a note on each held pull request that has none,
              and edit a note whose verdict changed. A current note is left alone.
  --summary   print the run summary (markdown) for GITHUB_STEP_SUMMARY.

Env: GH_TOKEN, GITHUB_REPOSITORY (owner/name), PR_NUMBER, RIVET_REPORT_PATH (the
text report), RIVET_REPORT_JSON (the JSON report; without it the note is the
text report, as before).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

try:
    from axiom.extensions.builtins.release.lander import verdict as _verdict
except ImportError:  # run without PYTHONPATH=src: the plain report note still posts
    _verdict = None

MARKER = "<!-- rivet-lander:warn-only -->"

_RULES = (
    "This comment changes nothing. "
    "The lander's act step runs from main after each merge and every six hours, "
    "and lands a green, unprotected, non-draft pull request with no pending "
    "human review and no changes-requested review. "
    "A protected path is held and is not landed. "
    "A pending human review or a changes-requested review waits."
)


def comment_body(report: str, verdict=None, charts: str = "") -> str:
    """One comment. With a verdict, it leads with this pull request's own verdict
    and leaves the backlog to the run summary; without one, it is the fenced report.
    Branded charts, when drawn, sit right under the first line."""
    chart_lines = [charts.rstrip("\n"), ""] if charts else []
    if verdict is not None and _verdict is not None:
        lines = [_verdict.marker(verdict), *chart_lines, *_verdict.note_lines(verdict), ""]
        lines.append(f"<sub>{_RULES} The full backlog is in the lander run's summary.</sub>")
        return "\n".join(lines) + "\n"
    text = report.strip()
    fence = "````"
    while fence in text:
        fence += "`"
    head = f"{MARKER}\n" + ("\n".join(chart_lines) + "\n" if chart_lines else "")
    return (
        f"{head}**RIVET lander report.** {_RULES}\n\n"
        f"<details><summary>Full lander report</summary>\n\n{fence}\n{text}\n{fence}\n\n</details>\n"
    )


def charts_for(repo: str, number: str, report_text: str = "", report: dict | None = None) -> str:
    """The branded chart block, or "" when it cannot be drawn or published.

    The charts module lives with the shared rivet-land action, so every repository
    draws the same charts. A failure here never stops the comment.
    """
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "actions" / "rivet-land" / "scripts"))
        import rivet_charts

        view = json.loads(
            _gh(["pr", "view", number, "--repo", repo, "--json", "headRefOid,statusCheckRollup"]) or "{}"
        )
        passed, failing, running = rivet_charts.check_counts(view.get("statusCheckRollup"))
        if report is not None:
            counts = rivet_charts.counts_from_report(report)
            group = rivet_charts.group_from_report(report, int(number))
        else:
            counts = rivet_charts.counts_from_text(report_text)
            group = rivet_charts.group_from_text(report_text, int(number))
        return rivet_charts.chart_block(
            repo=repo,
            pr=int(number),
            head_sha=str(view.get("headRefOid") or ""),
            group=group,
            passed=passed,
            failing=failing,
            running=running,
            counts=counts,
        )
    except (Exception, SystemExit) as exc:  # noqa: BLE001 - the comment matters more than its charts
        print(f"::warning::RIVET charts were not drawn: {exc}", file=sys.stderr)
        return ""


def _redact(text: str) -> str:
    redacted = text
    for name in ("GH_TOKEN", "GITHUB_TOKEN", "RELEASE_FIX_WEBHOOK_KEY"):
        secret = os.environ.get(name) or ""
        if secret:
            redacted = redacted.replace(secret, "[redacted]")
    return redacted


def _gh(args: list[str], *, payload: dict | None = None) -> str:
    done = subprocess.run(
        ["gh", *args],
        input=None if payload is None else json.dumps(payload),
        text=True,
        capture_output=True,
        check=False,
    )
    if done.returncode != 0:
        detail = _redact((done.stderr or done.stdout or "gh failed").strip())
        raise SystemExit(detail or "gh failed")
    return done.stdout


def existing_comment(repo: str, number: str) -> tuple[str, str]:
    """The first comment carrying MARKER's prefix, as ``(id, body)``; empty when none."""
    needle = json.dumps(MARKER[: MARKER.index(" -->")])
    raw = _gh(
        [
            "api",
            "--paginate",
            f"repos/{repo}/issues/{number}/comments",
            "--jq",
            f".[] | select(.body | contains({needle})) | {{id, body}}",
        ]
    )
    for line in raw.splitlines():
        line = line.strip()
        if line:
            row = json.loads(line)
            return str(row.get("id") or ""), str(row.get("body") or "")
    return "", ""


def publish(repo: str, number: str, body: str, comment_id: str | None = None) -> None:
    if comment_id is None:
        comment_id, _ = existing_comment(repo, number)
    payload = {"body": body}
    if comment_id:
        _gh(
            [
                "api",
                "--method",
                "PATCH",
                f"repos/{repo}/issues/comments/{comment_id}",
                "--input",
                "-",
            ],
            payload=payload,
        )
        return
    _gh(
        ["api", "--method", "POST", f"repos/{repo}/issues/{number}/comments", "--input", "-"],
        payload=payload,
    )


def _load_json(path: str) -> dict | None:
    if not path or _verdict is None:
        return None
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def refresh(repo: str, report: dict) -> int:
    """From main: one note per held pull request, edited only when its verdict changed."""
    verdicts = {v.number: v for v in _verdict.all_verdicts(report)}
    existing: dict[int, str] = {}
    ids: dict[int, str] = {}
    for number in verdicts:
        comment_id, body = existing_comment(repo, str(number))
        if comment_id:
            existing[number] = _verdict.key_in(body)
            ids[number] = comment_id
    plan = _verdict.refresh_plan(report, existing)
    for number, action in plan:
        charts = charts_for(repo, str(number), report=report)
        body = comment_body("", verdict=verdicts[number], charts=charts)
        publish(repo, str(number), body, comment_id=ids.get(number, ""))
        print(f"#{number}: {'edited the note' if action == 'edit' else 'posted a note'}")
    if not plan:
        print("every note is current")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    repo = os.environ.get("GITHUB_REPOSITORY", "").strip()
    path = os.environ.get("RIVET_REPORT_PATH", "").strip()
    report = _load_json(os.environ.get("RIVET_REPORT_JSON", "").strip())
    if "--summary" in args:
        if report is None:
            return 0
        text = Path(path).read_text(encoding="utf-8") if path and Path(path).is_file() else ""
        print(_verdict.summary_markdown(report, text), end="")
        return 0
    if "--refresh" in args:
        if not repo or report is None:
            print("GITHUB_REPOSITORY and RIVET_REPORT_JSON are required", file=sys.stderr)
            return 1
        return refresh(repo, report)
    number = os.environ.get("PR_NUMBER", "").strip()
    if not repo or not number or not path:
        print("GITHUB_REPOSITORY, PR_NUMBER, and RIVET_REPORT_PATH are required", file=sys.stderr)
        return 1
    found = _verdict.verdict_for(report, int(number)) if report is not None else None
    text = Path(path).read_text(encoding="utf-8")
    body = comment_body(text, verdict=found, charts=charts_for(repo, number, text, report))
    publish(repo, number, body)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
