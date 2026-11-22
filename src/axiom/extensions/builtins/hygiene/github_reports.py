# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""GitHub hygiene: stale drafts, notifications, packages.

Tier 0 posts a 7-day notice and label on stale drafts. Tier 1 closes
bot-authored or TIDY-owned drafts after that notice expires with no
activity, and marks notifications read when the subject is already
merged or closed. Closing a human's PR, deleting packages, or anything
outside policy is tier 2 (Needs you).
"""

from __future__ import annotations

import json
import logging
import subprocess
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from typing import Any

_log = logging.getLogger("axiom.hygiene.github_reports")

GhRunner = Callable[..., Any]

NOTICE_MARKER = "<!-- tidy-stale-notice -->"
NOTICE_LABEL = "tidy/stale-notice"
NOTICE_AT_PREFIX = "<!-- tidy-noticed-at:"
DEFAULT_STALE_DAYS = 7
ACTIVITY_GRACE = timedelta(minutes=5)

_BOT_LOGINS = frozenset({
    "dependabot",
    "dependabot[bot]",
    "github-actions",
    "github-actions[bot]",
    "tidy",
    "tidy[bot]",
    "axiom-tidy",
    "axiom-tidy[bot]",
    "renovate",
    "renovate[bot]",
    "pre-commit-ci[bot]",
    "copilot",
    "copilot[bot]",
})


def _run(runner: GhRunner, args: list[str]) -> tuple[int, str]:
    result = runner(args, capture_output=True, text=True, check=False)
    return result.returncode, result.stdout or ""


def _gh_json(runner: GhRunner, path: str, default: Any = None, *, method: str = "GET", raw: list[str] | None = None) -> Any:
    args = ["gh", "api"]
    if method and method.upper() != "GET":
        args.extend(["-X", method.upper()])
    args.append(path)
    if raw:
        args.extend(raw)
    code, out = _run(runner, args)
    if code != 0 or not out.strip():
        return default
    try:
        return json.loads(out)
    except json.JSONDecodeError:
        return default


def _gh_ok(runner: GhRunner, path: str, *, method: str = "GET", raw: list[str] | None = None) -> bool:
    args = ["gh", "api"]
    if method and method.upper() != "GET":
        args.extend(["-X", method.upper()])
    args.append(path)
    if raw:
        args.extend(raw)
    code, _out = _run(runner, args)
    return code == 0


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def is_bot_or_tidy(login: str | None, type_: str | None = None) -> bool:
    name = (login or "").strip().lower()
    if (type_ or "").strip().lower() == "bot":
        return True
    if not name:
        return False
    return name in _BOT_LOGINS or name.endswith("[bot]")


def notice_body(*, stale_days: int, now: datetime) -> str:
    noticed = now.astimezone(timezone.utc).isoformat()
    return (
        f"{NOTICE_MARKER}\n"
        f"{NOTICE_AT_PREFIX} {noticed} -->\n\n"
        f"This draft has had no activity for {stale_days} days. "
        f"If it is bot-authored or TIDY-owned and nothing happens for another "
        f"{stale_days} days, TIDY will close it. Human-authored drafts stay "
        f"open unless a person approves closing. Leave a comment or push a "
        f"commit to keep it."
    )


def stale_drafts(
    repo: str,
    *,
    now: datetime,
    stale_days: int = DEFAULT_STALE_DAYS,
    runner: GhRunner = subprocess.run,
) -> list[dict[str, Any]]:
    """Open draft pull requests not updated in ``stale_days``."""
    pulls = _gh_json(runner, f"repos/{repo}/pulls?state=open&per_page=100", default=[])
    if not isinstance(pulls, list):
        return []
    out: list[dict[str, Any]] = []
    cutoff = stale_days * 86400
    for pr in pulls:
        if not isinstance(pr, dict) or not pr.get("draft"):
            continue
        updated = _parse_dt(pr.get("updated_at"))
        if updated is None or (now - updated).total_seconds() < cutoff:
            continue
        labels = [str(lab.get("name") or "") for lab in (pr.get("labels") or []) if isinstance(lab, dict)]
        user = pr.get("user") or {}
        if not isinstance(user, dict):
            user = {}
        out.append({
            "repo": repo,
            "number": pr.get("number"),
            "title": pr.get("title") or "",
            "updated_at": pr.get("updated_at"),
            "labels": labels,
            "html_url": pr.get("html_url") or "",
            "author": user.get("login") or "",
            "author_type": user.get("type") or "",
            "user": user.get("login") or "",
        })
    return out


def list_issue_comments(repo: str, number: int, *, runner: GhRunner) -> list[dict[str, Any]]:
    payload = _gh_json(runner, f"repos/{repo}/issues/{int(number)}/comments?per_page=100", default=[])
    if not isinstance(payload, list):
        return []
    return [c for c in payload if isinstance(c, dict)]


def notice_posted_at(comments: list[dict[str, Any]]) -> datetime | None:
    for comment in comments:
        body = str(comment.get("body") or "")
        if NOTICE_MARKER not in body:
            continue
        for line in body.splitlines():
            if line.strip().startswith(NOTICE_AT_PREFIX):
                raw = line.strip()[len(NOTICE_AT_PREFIX):].replace("-->", "").strip()
                parsed = _parse_dt(raw)
                if parsed is not None:
                    return parsed
        return _parse_dt(comment.get("created_at")) or _parse_dt(comment.get("updated_at"))
    return None


def has_activity_after(comments: list[dict[str, Any]], noticed_at: datetime, *, updated_at: datetime | None) -> bool:
    for comment in comments:
        body = str(comment.get("body") or "")
        if NOTICE_MARKER in body:
            continue
        created = _parse_dt(comment.get("created_at"))
        if created is not None and created > noticed_at:
            return True
    if updated_at is not None and updated_at > noticed_at + ACTIVITY_GRACE:
        return True
    return False


def post_stale_notice(repo: str, number: int, *, stale_days: int, now: datetime, runner: GhRunner) -> bool:
    body = notice_body(stale_days=stale_days, now=now)
    return _gh_ok(
        runner,
        f"repos/{repo}/issues/{int(number)}/comments",
        method="POST",
        raw=["-f", f"body={body}"],
    )


def add_notice_label(repo: str, number: int, *, runner: GhRunner) -> bool:
    # GitHub accepts a JSON array of label names.
    return _gh_ok(
        runner,
        f"repos/{repo}/issues/{int(number)}/labels",
        method="POST",
        raw=["-f", f"labels[]={NOTICE_LABEL}"],
    )


def close_pull(repo: str, number: int, *, runner: GhRunner) -> bool:
    return _gh_ok(
        runner,
        f"repos/{repo}/pulls/{int(number)}",
        method="PATCH",
        raw=["-f", "state=closed"],
    )


def notification_report(
    *,
    runner: GhRunner = subprocess.run,
) -> dict[str, Any]:
    """Unread GitHub notifications, including subject URLs for state checks."""
    notes = _gh_json(runner, "notifications?all=false&per_page=50", default=[])
    if not isinstance(notes, list):
        return {"ok": False, "unread": 0, "items": [], "error": "could not list notifications"}
    items = []
    for note in notes:
        if not isinstance(note, dict):
            continue
        subject = note.get("subject") or {}
        if not isinstance(subject, dict):
            subject = {}
        items.append({
            "id": str(note.get("id") or ""),
            "reason": note.get("reason") or "",
            "unread": bool(note.get("unread", True)),
            "repository": ((note.get("repository") or {}).get("full_name") or ""),
            "title": (subject.get("title") or ""),
            "updated_at": note.get("updated_at") or "",
            "subject_type": subject.get("type") or "",
            "subject_url": subject.get("url") or "",
        })
    return {"ok": True, "unread": len(items), "items": items}


def subject_is_merged_or_closed(subject_url: str, *, runner: GhRunner) -> bool:
    if not subject_url:
        return False
    path = subject_url
    marker = "api.github.com/"
    if marker in subject_url:
        path = subject_url.split(marker, 1)[1]
    if path.startswith("https://") or path.startswith("http://"):
        return False
    payload = _gh_json(runner, path, default=None)
    if not isinstance(payload, dict):
        return False
    return (payload.get("state") == "closed") or bool(payload.get("merged_at") or payload.get("merged"))


def mark_notification_read(thread_id: str, *, runner: GhRunner) -> bool:
    if not thread_id:
        return False
    return _gh_ok(runner, f"notifications/threads/{thread_id}", method="PATCH")


def package_report(
    org: str,
    *,
    runner: GhRunner = subprocess.run,
    package_types: tuple[str, ...] = ("container", "npm", "maven", "pip"),
) -> dict[str, Any]:
    """Best-effort Packages inventory. A missing read:packages scope is reported, not raised."""
    packages: list[dict[str, Any]] = []
    errors: list[str] = []
    for ptype in package_types:
        raw = _run(runner, ["gh", "api", f"orgs/{org}/packages?package_type={ptype}"])
        code, out = raw
        if code != 0:
            text = out.strip() or "request failed"
            if "403" in text or "read:packages" in text or "scope" in text.lower():
                errors.append(f"{ptype}: missing read:packages scope")
            else:
                errors.append(f"{ptype}: unavailable")
            continue
        try:
            payload = json.loads(out) if out.strip() else []
        except json.JSONDecodeError:
            errors.append(f"{ptype}: invalid response")
            continue
        if not isinstance(payload, list):
            continue
        for pkg in payload:
            if not isinstance(pkg, dict):
                continue
            packages.append({
                "name": pkg.get("name") or "",
                "package_type": ptype,
                "visibility": pkg.get("visibility") or "",
                "updated_at": pkg.get("updated_at") or "",
            })
    return {"ok": not errors or bool(packages), "packages": packages, "errors": errors}
