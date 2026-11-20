#!/usr/bin/env python3
"""Require a Hold: comment on every do-not-merge pull request.

A comment counts when its body, after leading whitespace, starts with
``Hold:``. It must state a reason and who or what releases the hold.
``Review-by: YYYY-MM-DD`` is optional. When it is omitted, the review-by
date is seven days after the comment.

The latest Hold: comment is the one that counts. This script never
removes labels. It writes one job summary of holds whose review-by date
is before today. A missing or invalid Hold: comment fails the job.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta

HOLD_LABEL = "do-not-merge"
DEFAULT_REVIEW_DAYS = 7


@dataclass(frozen=True)
class Comment:
    body: str
    created_at: datetime


@dataclass(frozen=True)
class Pull:
    number: int
    title: str
    url: str
    labels: tuple[str, ...]
    comments: tuple[Comment, ...]


@dataclass(frozen=True)
class Hold:
    number: int
    title: str
    url: str
    reason: str
    releases: str
    review_by: date
    commented_at: datetime


@dataclass(frozen=True)
class Report:
    failures: tuple[str, ...]
    overdue: tuple[Hold, ...]
    summary: str


def _aware(moment: datetime) -> datetime:
    if moment.tzinfo is None:
        return moment.replace(tzinfo=UTC)
    return moment.astimezone(UTC)


def parse_time(value: str) -> datetime:
    text = value.strip().replace("Z", "+00:00")
    return _aware(datetime.fromisoformat(text))


def parse_hold_body(
    body: str, commented_at: datetime
) -> tuple[tuple[str, str, date] | None, str | None]:
    """Return ``(reason, releases, review_by)`` or an error.

    ``(None, None)`` means the body is not a Hold: comment.
    """
    stripped = body.lstrip()
    if not stripped.startswith("Hold:"):
        return None, None
    lines = stripped.splitlines()
    reason = lines[0][len("Hold:") :].strip()
    if not reason:
        return None, "the Hold: line needs a reason"
    releases = ""
    review_by: date | None = None
    saw_review_by = False
    for line in lines[1:]:
        text = line.strip()
        if text.startswith("Releases:"):
            releases = text[len("Releases:") :].strip()
        elif text.startswith("Review-by:"):
            saw_review_by = True
            raw = text[len("Review-by:") :].strip()
            try:
                review_by = date.fromisoformat(raw)
            except ValueError:
                return None, "Review-by must be YYYY-MM-DD"
    if not releases:
        return None, "a Releases: line must name who or what releases the hold"
    if review_by is None:
        if saw_review_by:
            return None, "Review-by must be YYYY-MM-DD"
        review_by = _aware(commented_at).date() + timedelta(days=DEFAULT_REVIEW_DAYS)
    return (reason, releases, review_by), None


def resolve_hold(
    comments: tuple[Comment, ...],
) -> tuple[tuple[str, str, date, datetime] | None, str | None]:
    """Use the latest Hold: comment. An invalid latest comment is an error."""
    found: list[tuple[datetime, tuple[str, str, date] | None, str | None]] = []
    for comment in comments:
        parsed, err = parse_hold_body(comment.body, comment.created_at)
        if parsed is None and err is None:
            continue
        found.append((_aware(comment.created_at), parsed, err))
    if not found:
        return None, "no comment starts with 'Hold:'"
    _at, parsed, err = max(found, key=lambda item: item[0])
    if err or parsed is None:
        return None, err or "the Hold: comment is incomplete"
    reason, releases, review_by = parsed
    return (reason, releases, review_by, _at), None


def missing_hold_message(number: int, detail: str) -> str:
    return (
        f"#{number} is labeled {HOLD_LABEL} and has no valid Hold: comment ({detail}). "
        "Add a pull request comment that starts with 'Hold:' and include the reason, "
        "a 'Releases:' line naming who or what releases the hold, and an optional "
        "'Review-by: YYYY-MM-DD'. If Review-by is omitted, it is 7 days after the comment. "
        "This check does not remove the label."
    )


def evaluate(pulls: tuple[Pull, ...], *, today: date, focus: int | None) -> Report:
    """Fail a labeled pull request that has no valid Hold: comment.

    ``focus`` is the triggering pull request. A scheduled run passes
    ``None`` and checks every open ``do-not-merge`` pull request. An
    overdue review-by date is reported and is not a failure.
    """
    failures: list[str] = []
    overdue: list[Hold] = []
    for pull in pulls:
        if HOLD_LABEL not in pull.labels:
            continue
        parsed, err = resolve_hold(pull.comments)
        if err or parsed is None:
            if focus is None or pull.number == focus:
                failures.append(missing_hold_message(pull.number, err or "missing Hold: comment"))
            continue
        reason, releases, review_by, commented_at = parsed
        hold = Hold(
            number=pull.number,
            title=pull.title,
            url=pull.url,
            reason=reason,
            releases=releases,
            review_by=review_by,
            commented_at=commented_at,
        )
        if review_by < today:
            overdue.append(hold)
    overdue.sort(key=lambda item: (item.review_by, item.number))
    return Report(tuple(failures), tuple(overdue), summary_markdown(tuple(overdue)))


def summary_markdown(overdue: tuple[Hold, ...]) -> str:
    lines = [
        "## do-not-merge holds past review-by",
        "",
        "This check does not remove labels.",
        "",
    ]
    if not overdue:
        lines.append("None.")
        lines.append("")
        return "\n".join(lines)
    lines.extend(
        [
            "| PR | Review-by | Releases | Reason |",
            "| --- | --- | --- | --- |",
        ]
    )
    for hold in overdue:
        title = hold.title.replace("|", "\\|")
        reason = hold.reason.replace("|", "\\|")
        releases = hold.releases.replace("|", "\\|")
        link = f"[#{hold.number} {title}]({hold.url})" if hold.url else f"#{hold.number} {title}"
        lines.append(f"| {link} | {hold.review_by.isoformat()} | {releases} | {reason} |")
    lines.append("")
    return "\n".join(lines)


def _gh_json(args: list[str]) -> object:
    proc = subprocess.run(  # noqa: S603
        ["gh", *args],
        check=False,
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "gh failed").strip()
        raise RuntimeError(detail)
    return json.loads(proc.stdout)


def _comments(raw: object) -> tuple[Comment, ...]:
    if not isinstance(raw, list):
        return ()
    found: list[Comment] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        created = item.get("createdAt") or item.get("created_at") or ""
        if not created:
            continue
        found.append(Comment(body=str(item.get("body") or ""), created_at=parse_time(str(created))))
    return tuple(found)


def _labels(raw: object) -> tuple[str, ...]:
    if not isinstance(raw, list):
        return ()
    names: list[str] = []
    for item in raw:
        if isinstance(item, str):
            names.append(item)
        elif isinstance(item, dict) and item.get("name"):
            names.append(str(item["name"]))
    return tuple(names)


def _pull(row: dict) -> Pull:
    return Pull(
        number=int(row["number"]),
        title=str(row.get("title") or ""),
        url=str(row.get("url") or ""),
        labels=_labels(row.get("labels")),
        comments=_comments(row.get("comments")),
    )


def load_pulls(repo: str, focus: int | None) -> tuple[Pull, ...]:
    """Read open do-not-merge pull requests. Read-only: labels are not removed."""
    listed = _gh_json(
        [
            "pr",
            "list",
            "--repo",
            repo,
            "--state",
            "open",
            "--label",
            HOLD_LABEL,
            "--limit",
            "200",
            "--json",
            "number,title,url,labels,comments",
        ]
    )
    rows = listed if isinstance(listed, list) else []
    by_number = {int(row["number"]): row for row in rows if isinstance(row, dict)}
    if focus and focus not in by_number:
        one = _gh_json(
            [
                "pr",
                "view",
                str(focus),
                "--repo",
                repo,
                "--json",
                "number,title,url,labels,comments",
            ]
        )
        if isinstance(one, dict) and HOLD_LABEL in _labels(one.get("labels")):
            by_number[focus] = one
    return tuple(_pull(row) for row in by_number.values())


def write_summary(text: str) -> None:
    path = os.environ.get("GITHUB_STEP_SUMMARY", "").strip()
    if not path:
        print(text)
        return
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(text)
        if not text.endswith("\n"):
            handle.write("\n")


def main() -> int:
    if not os.environ.get("GH_TOKEN"):
        print("GH_TOKEN is required to read do-not-merge holds.", file=sys.stderr)
        return 1 if os.environ.get("GITHUB_ACTIONS") else 0
    repo = (os.environ.get("REPO") or os.environ.get("GITHUB_REPOSITORY") or "").strip()
    if not repo:
        print("REPO is required to read do-not-merge holds.", file=sys.stderr)
        return 1
    raw_pr = (os.environ.get("PR_NUMBER") or "").strip()
    focus = int(raw_pr) if raw_pr.isdigit() else None
    today = datetime.now(UTC).date()
    try:
        pulls = load_pulls(repo, focus)
    except (RuntimeError, json.JSONDecodeError, KeyError, ValueError) as exc:
        print(f"could not read do-not-merge holds: {exc}", file=sys.stderr)
        return 1
    report = evaluate(pulls, today=today, focus=focus)
    write_summary(report.summary)
    for failure in report.failures:
        print(failure, file=sys.stderr)
    return 1 if report.failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
