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
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "actions" / "rivet-land" / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import rivet_identity  # noqa: E402 - the app-identity notice (#2341), standard library only

try:
    import rivet_note as _note
except ImportError:  # the shared action is missing: the plain report note still posts
    _note = None

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


def comment_body(report: str, verdict=None, charts: str = "", facts=None) -> str:
    """One comment. With a verdict, it leads with this pull request's own verdict and
    leaves the backlog to the run summary; without one, it is the fenced report. With
    ``facts`` the verdict is checked against what RIVET actually did, carries when it was
    written, and links to the exact pages."""
    chart_lines = [charts.rstrip("\n"), ""] if charts else []
    if verdict is not None and _verdict is not None:
        if facts is not None and _note is not None:
            state = _note.honest_state(verdict.state, facts)
            key = _note_key(verdict, facts, state)
            return "\n".join([f"<!-- rivet-lander:warn-only key={key} -->", *chart_lines, _note.render(verdict, facts)])
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


def _note_key(verdict, facts, state: str) -> str:
    """The note's key: its honest state, why, and what is failing. A different key is a
    different note; an unchanged one is still rewritten once it is two hours old."""
    if facts.state in {"merged", "closed"}:
        return _note.terminal_key(facts.state)
    return _verdict._key(state, (*verdict.why, *(c.name for c in facts.failing)))


_STRIP_WORD = {"stalled": ("Stalled", "warn")}


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _parse(value) -> datetime | None:
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


_PR_FIELDS = "state,mergedAt,mergedBy,mergeCommit,statusCheckRollup,commits"
_RED = {"FAILURE", "CANCELLED", "TIMED_OUT", "ACTION_REQUIRED", "ERROR"}


def with_not_run(facts, report: dict | None, number: int | str):
    """The facts, plus the report's "tests: not run" sentence and fix when this head has them,
    so the note and its image never show a pass count that leaves the required checks out."""
    from dataclasses import replace

    for row in (report or {}).get("tests_not_run") or ():
        if int(row.get("number", 0)) == int(number):
            return replace(facts, tests_not_run=str(row.get("sentence") or ""), tests_not_run_fix=str(row.get("fix") or ""))
    return facts


def facts_for(repo: str, number: str, now: datetime | None = None):
    """What is true of this pull request now: its failing checks (each with its own job
    link), its counts, and the last thing RIVET did to it. Nothing is assumed."""
    import rivet_charts

    now = now or _utcnow()
    view = json.loads(_gh(["pr", "view", number, "--repo", repo, "--json", _PR_FIELDS]) or "{}")
    rollup = view.get("statusCheckRollup")
    failing = [
        _note.Check(str(c.get("name") or c.get("context") or "check"), str(c.get("detailsUrl") or c.get("targetUrl") or ""))
        for c in rivet_charts._latest_rows(rollup)
        if str(c.get("conclusion") or c.get("state") or "").upper() in _RED
    ]
    passed, _failing, running = rivet_charts.check_counts(rollup)
    actions: list = []
    for commit in view.get("commits") or []:
        logins = [str(a.get("login") or "") for a in commit.get("authors") or []]
        when = _parse(commit.get("committedDate"))
        if when and commit.get("messageHeadline", "").startswith("Merge branch") and any(x.endswith("[bot]") for x in logins):
            actions.append(_note.Action(when, "updated the branch from main", f"https://github.com/{repo}/commit/{commit.get('oid')}"))
    try:
        refs = json.loads(_gh(["api", f"repos/{repo}/git/matching-refs/heads/rivet-fix/{number}-"]) or "[]")
    except SystemExit:
        refs = []
    for ref in refs:
        sha = (ref.get("object") or {}).get("sha", "")
        branch = str(ref.get("ref", "")).removeprefix("refs/heads/")
        try:
            when = _parse(json.loads(_gh(["api", f"repos/{repo}/commits/{sha}"]))["commit"]["committer"]["date"])
        except (SystemExit, KeyError, ValueError):
            continue
        if when:
            actions.append(_note.Action(when, "pushed a fix attempt", f"https://github.com/{repo}/compare/main...{branch}"))
    server = os.environ.get("GITHUB_SERVER_URL", "https://github.com")
    run_id = os.environ.get("GITHUB_RUN_ID", "")
    return _note.Facts(
        repo=repo, number=int(number), now=now, state=str(view.get("state") or "OPEN").lower(),
        run_url=f"{server}/{repo}/actions/runs/{run_id}" if run_id else "",
        failing=failing, passed=passed, running=running,
        last_action=max(actions, key=lambda a: a.at) if actions else None,
        merged_at=_parse(view.get("mergedAt")), merged_by=str((view.get("mergedBy") or {}).get("login") or ""),
        merge_sha=str((view.get("mergeCommit") or {}).get("oid") or ""),
    )


def charts_for(repo: str, number: str, report_text: str = "", report: dict | None = None, facts=None) -> str:
    """The note's one status image, or "" when it cannot be drawn or published.

    The charts module lives with the shared rivet-land action, so every repository
    draws the same strip. A failure here never stops the comment.
    """
    try:
        import rivet_charts

        if facts is None:
            facts = facts_for(repo, number)
        if facts.state != "open":
            return ""
        group = (
            rivet_charts.group_from_report(report, int(number))
            if report is not None
            else rivet_charts.group_from_text(report_text, int(number))
        )
        word, tone = _STRIP_WORD.get(_note.honest_state(group, facts), ("", ""))
        view = json.loads(_gh(["pr", "view", number, "--repo", repo, "--json", "headRefOid"]) or "{}")
        return rivet_charts.chart_block(
            repo=repo, pr=int(number), head_sha=str(view.get("headRefOid") or ""), group=group,
            passed=facts.passed, failing=len(facts.failing), running=facts.running,
            word=word, tone=tone, link=_note.strip_link(facts), not_run=bool(facts.tests_not_run),
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


def existing_note(repo: str, number: str) -> tuple[str, str, str]:
    """The first comment carrying MARKER's prefix, as ``(id, body, updated_at)``; empty when none."""
    needle = json.dumps(MARKER[: MARKER.index(" -->")])
    raw = _gh(
        [
            "api",
            "--paginate",
            f"repos/{repo}/issues/{number}/comments",
            "--jq",
            f".[] | select(.body | contains({needle})) | {{id, body, updated_at}}",
        ]
    )
    for line in raw.splitlines():
        line = line.strip()
        if line:
            row = json.loads(line)
            return str(row.get("id") or ""), str(row.get("body") or ""), str(row.get("updated_at") or "")
    return "", "", ""


def existing_comment(repo: str, number: str) -> tuple[str, str]:
    """The first comment carrying MARKER's prefix, as ``(id, body)``; empty when none."""
    found = existing_note(repo, number)
    return found[0], found[1]


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


def _closed_numbers(repo: str) -> list[int]:
    """Recently closed or merged pull requests, so a note that still claims otherwise is finished."""
    raw = _gh(["pr", "list", "--repo", repo, "--state", "closed", "--limit", "40", "--json", "number"])
    return [int(row["number"]) for row in json.loads(raw or "[]")]


def _write(repo: str, number: int, verdict, report: dict | None, facts, comment_id: str, label: str) -> None:
    facts = with_not_run(facts, report, number)
    body = comment_body("", verdict=verdict, charts=charts_for(repo, str(number), report=report, facts=facts), facts=facts)
    publish(repo, str(number), body, comment_id=comment_id)
    print(f"#{number}: {label}")


def refresh(repo: str, report: dict) -> int:
    """From main: keep every note true. A note is rewritten when its verdict changed,
    when it is two hours old, or (once) when its pull request merged or closed; a held
    pull request with no note gets one, unless it is a draft."""
    now = _utcnow()
    verdicts = {v.number: v for v in _verdict.all_verdicts(report)}
    wrote = 0
    for number, verdict in verdicts.items():
        comment_id, body, updated = existing_note(repo, str(number))
        if not comment_id:
            if verdict.state in _verdict._HELD_STATES and not verdict.draft:
                facts = facts_for(repo, str(number), now)
                _write(repo, number, verdict, report, facts, "", "posted a note")
                wrote += 1
            continue
        facts = with_not_run(facts_for(repo, str(number), now), report, number)
        key = _note_key(verdict, facts, _note.honest_state(verdict.state, facts))
        when = _parse(updated) or now
        if _note.needs_rewrite(existing_key=_verdict.key_in(body), new_key=key, updated_at=when, now=now, state="open"):
            _write(repo, number, verdict, report, facts, comment_id, "edited the note")
            wrote += 1
    for number in _closed_numbers(repo):
        if number in verdicts:
            continue
        comment_id, body, _updated = existing_note(repo, str(number))
        if not comment_id:
            continue
        facts = facts_for(repo, str(number), now)
        if _note.needs_rewrite(existing_key=_verdict.key_in(body), new_key=_note.terminal_key(facts.state),
                               updated_at=now, now=now, state=facts.state):
            done = _verdict.Verdict(number, "", "lands", "", (), (), "nobody", False, "")
            _write(repo, number, done, None, facts, comment_id, f"finished the note ({facts.state})")
            wrote += 1
    if not wrote:
        print("every note is current")
    return 0


def _with_identity_notice(summary: str) -> str:
    """Put "running without the RIVET identity: ..." in the summary's first lines when the app
    token step failed (#2341). It follows the can-merge line, which stays the first line."""
    said = rivet_identity.notice()
    if not said:
        return summary
    first, _, rest = summary.partition("\n")
    if first.startswith("## "):
        return f"{said}\n\n{summary}"
    return f"{first}\n{said}\n{rest}"


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    repo = os.environ.get("GITHUB_REPOSITORY", "").strip()
    path = os.environ.get("RIVET_REPORT_PATH", "").strip()
    report = _load_json(os.environ.get("RIVET_REPORT_JSON", "").strip())
    if "--summary" in args:
        if report is None:
            return 0
        text = Path(path).read_text(encoding="utf-8") if path and Path(path).is_file() else ""
        print(_with_identity_notice(_verdict.summary_markdown(report, text)), end="")
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
    facts = with_not_run(facts_for(repo, number), report, number) if found is not None else None
    body = comment_body(text, verdict=found, charts=charts_for(repo, number, text, report, facts), facts=facts)
    publish(repo, number, body)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
