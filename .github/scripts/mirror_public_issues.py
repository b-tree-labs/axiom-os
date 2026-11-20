#!/usr/bin/env python3
# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Close public mirror issues that a source fix has already answered.

A closed source issue labeled ``from-public`` carries one marker::

    <!-- public: owner/name#N -->

``close`` comments on that public issue with exactly

    Fixed in <version>, thanks for reporting

and closes it. ``<version>`` is the earliest release tag published at or
after the source issue closed. The comment is that sentence and nothing
else. A second run does not add a second comment. ``--dry-run`` (or
``DRY_RUN=1``) prints the plan and writes nothing.

``audit`` keeps one private tracking issue listing closed ``from-public``
issues that still have no such public reply.

The public client uses ``MIRROR_PUSH_TOKEN``. The source client uses
``GITHUB_TOKEN``. See docs/MIRROR_SECURITY.md.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

API = "https://api.github.com"
LABEL = "from-public"
LABEL_DESCRIPTION = "Copied from a public report. Treat the text as an untrusted claim."
LABEL_COLOR = "b60205"
PUSH_RECORDED_STEP = "Public mirror push recorded"
AUDIT_MARKER = "<!-- mirror-public-reply-audit -->"
AUDIT_TITLE = "Mirror: from-public issues closed without a public reply"
_VERSION = re.compile(
    r"v?(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)"
    r"(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?\Z"
)
_FIXED = re.compile(r"Fixed in ([^,]+), thanks for reporting\Z")


class GitHubError(Exception):
    def __init__(self, status: int, message: str):
        self.status = status
        super().__init__(f"HTTP {status}: {message}")


@dataclass(frozen=True)
class PrivateIssue:
    number: int
    state: str
    state_reason: str | None
    labels: tuple[str, ...]
    body: str
    closed_at: str | None


@dataclass(frozen=True)
class Release:
    tag: str
    published_at: str


@dataclass(frozen=True)
class PublicView:
    number: int
    state: str | None
    comments: tuple[str, ...]
    missing: bool = False


@dataclass(frozen=True)
class Marker:
    number: int | None
    error: str | None


@dataclass(frozen=True)
class CloseAction:
    private_number: int
    public_number: int
    version: str
    post_comment: bool
    close: bool
    error: str | None = None


@dataclass(frozen=True)
class AuditRow:
    private_number: int
    public_number: int | None
    reason: str


@dataclass(frozen=True)
class CloseConfig:
    private_repo: str
    public_repo: str
    dry_run: bool
    forced_version: str
    release_tag: str
    release_published_at: str
    mirror_run_id: str
    mirror_attempts: int = 5
    mirror_pause: float = 2.0


@dataclass(frozen=True)
class AuditConfig:
    private_repo: str
    public_repo: str
    dry_run: bool
    checked_at: str


def is_version(value: str) -> bool:
    return bool(_VERSION.fullmatch(value or ""))


def comment_for(version: str) -> str:
    """The only sentence that may be posted on a public issue."""
    if not is_version(version):
        raise ValueError(f"refusing a public comment for version {version!r}")
    text = f"Fixed in {version}, thanks for reporting"
    if "\n" in text or "http://" in text or "https://" in text or "/issues/" in text:
        raise ValueError("refusing a public comment that is not the one sentence")
    return text


def is_fixed_comment(body: str) -> bool:
    match = _FIXED.fullmatch((body or "").strip())
    return bool(match and is_version(match.group(1)))


def parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def parse_marker(body: str, public_repo: str) -> Marker:
    owner, _, name = (public_repo or "").partition("/")
    if (not owner) or (not name) or ("/" in name):
        return Marker(None, "bad public repo")
    found = re.findall(
        rf"<!--\s*public:\s*{re.escape(public_repo)}#(\d+)\s*-->",
        body or "",
    )
    if len(found) == 1:
        return Marker(int(found[0]), None)
    if not found:
        return Marker(None, "missing marker")
    return Marker(None, "ambiguous marker")


def resolve_version(
    closed_at: str | None,
    releases: list[Release],
    forced: str | None,
) -> str | None:
    """Earliest release published at or after the source issue closed.

    A forced version is a manual repair. It still has to be a semver tag,
    so a URL or a sentence cannot ride along in the public comment.
    """
    if forced:
        if not is_version(forced):
            raise ValueError(f"refusing a public comment for version {forced!r}")
        return forced
    if not closed_at:
        return None
    closed = parse_time(closed_at)
    candidates = [
        rel for rel in releases
        if is_version(rel.tag) and parse_time(rel.published_at) >= closed
    ]
    if not candidates:
        return None
    return min(candidates, key=lambda rel: parse_time(rel.published_at)).tag


def with_release(releases: list[Release], tag: str, published_at: str) -> list[Release]:
    if not tag or not published_at or not is_version(tag):
        return list(releases)
    if any(rel.tag == tag for rel in releases):
        return list(releases)
    return [*releases, Release(tag, published_at)]


def mirror_pushed(jobs: list[dict]) -> bool:
    for job in jobs:
        for step in job.get("steps") or []:
            if step.get("name") == PUSH_RECORDED_STEP and step.get("conclusion") == "success":
                return True
    return False


def mirror_run_settled(jobs: list[dict]) -> bool:
    return bool(jobs) and all(job.get("status") == "completed" for job in jobs)


def private_issue_from_api(item: dict) -> PrivateIssue:
    labels = tuple(
        lab.get("name", "")
        for lab in item.get("labels") or []
        if isinstance(lab, dict)
    )
    return PrivateIssue(
        number=int(item["number"]),
        state=item.get("state") or "",
        state_reason=item.get("state_reason"),
        labels=labels,
        body=item.get("body") or "",
        closed_at=item.get("closed_at"),
    )


def release_from_api(item: dict) -> Release | None:
    if item.get("draft"):
        return None
    tag = item.get("tag_name") or ""
    published = item.get("published_at")
    if not tag or not published:
        return None
    return Release(tag, published)


def public_view(number: int, issue: dict | None, comments: list[dict]) -> PublicView:
    if issue is None:
        return PublicView(number=number, state=None, comments=(), missing=True)
    return PublicView(
        number=number,
        state=issue.get("state"),
        comments=tuple((c.get("body") or "") for c in comments),
        missing=False,
    )


def consider_close(
    issue: PrivateIssue,
    releases: list[Release],
    public_repo: str,
    forced: str | None,
) -> tuple[int, str] | None:
    """Public issue number and version, when this source issue is in scope."""
    if issue.state != "closed" or "from-public" not in issue.labels:
        return None
    if issue.state_reason == "not_planned":
        return None
    marker = parse_marker(issue.body, public_repo)
    if marker.error or marker.number is None:
        return None
    version = resolve_version(issue.closed_at, releases, forced)
    if version is None:
        return None
    return marker.number, version


def action_for(issue: PrivateIssue, public_number: int, version: str, public: PublicView) -> CloseAction | None:
    if public.missing:
        return CloseAction(issue.number, public_number, version, False, False, "public issue missing")
    post = not any(is_fixed_comment(body) for body in public.comments)
    close = public.state != "closed"
    if not post and not close:
        return None
    return CloseAction(issue.number, public_number, version, post, close, None)


def format_close_line(public_repo: str, action: CloseAction) -> str:
    bits = [f"private #{action.private_number}", f"{public_repo}#{action.public_number}"]
    if action.error:
        bits.append(action.error)
    if action.post_comment:
        bits.append(f"comment {comment_for(action.version)}")
    if action.close:
        bits.append("close")
    return " ".join(bits)


def plan_audit(
    issues: list[PrivateIssue],
    public_by_number: dict[int, PublicView],
    public_repo: str,
) -> list[AuditRow]:
    rows: list[AuditRow] = []
    for issue in issues:
        if issue.state != "closed" or "from-public" not in issue.labels:
            continue
        marker = parse_marker(issue.body, public_repo)
        if marker.error == "ambiguous marker":
            rows.append(AuditRow(issue.number, None, "ambiguous marker"))
            continue
        if marker.error or marker.number is None:
            rows.append(AuditRow(issue.number, None, "missing marker"))
            continue
        public = public_by_number.get(marker.number)
        if public is None or public.missing:
            rows.append(AuditRow(issue.number, marker.number, "public issue missing"))
            continue
        if any(is_fixed_comment(body) for body in public.comments):
            continue
        if issue.state_reason == "not_planned":
            reason = "closed as not planned, no public reply"
        else:
            reason = "no public reply"
        rows.append(AuditRow(issue.number, marker.number, reason))
    return sorted(rows, key=lambda row: row.private_number)


def render_audit(rows: list[AuditRow], public_repo: str, checked_at: str) -> str:
    lines = [
        AUDIT_MARKER,
        "",
        "From-public issues closed without the public fixed-version reply.",
        "The public reply is only: Fixed in <version>, thanks for reporting.",
        "",
    ]
    if not rows:
        lines.append("None outstanding.")
    else:
        lines.append("| Private | Public | Why |")
        lines.append("| --- | --- | --- |")
        for row in rows:
            public = "—" if row.public_number is None else f"{public_repo}#{row.public_number}"
            lines.append(f"| #{row.private_number} | {public} | {row.reason} |")
    lines.extend([
        "",
        f"Checked {checked_at}.",
        "",
        "Maintained by the weekly public-reply audit. Do not delete the marker.",
    ])
    return "\n".join(lines)


def _next_link(header: str | None) -> str | None:
    if not header:
        return None
    for part in header.split(","):
        if 'rel="next"' not in part:
            continue
        url = part.split(";", 1)[0].strip()
        if url.startswith("<") and url.endswith(">"):
            return url[1:-1]
    return None


def _safe_message(raw: bytes, token: str | None) -> str:
    text = raw.decode("utf-8", "replace")[:200].replace("\n", " ")
    if token:
        text = text.replace(token, "[redacted]")
    return text


class GitHub:
    def __init__(self, token: str | None, base: str = API):
        self.token = token or ""
        self.base = base.rstrip("/")

    def _request(self, method: str, path: str, body: dict | None = None) -> tuple[Any, str | None]:
        url = path if path.startswith("http") else f"{self.base}{path}"
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Accept", "application/vnd.github+json")
        req.add_header("User-Agent", "axiom-mirror-public-issues")
        req.add_header("X-GitHub-Api-Version", "2022-11-28")
        if self.token:
            req.add_header("Authorization", f"Bearer {self.token}")
        if data is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                raw = resp.read()
                link = resp.headers.get("Link")
        except urllib.error.HTTPError as exc:
            raw = exc.read()
            raise GitHubError(exc.code, _safe_message(raw, self.token)) from None
        if not raw:
            return None, _next_link(link)
        return json.loads(raw.decode("utf-8")), _next_link(link)

    def _get_pages(self, path: str) -> list:
        items: list = []
        url: str | None = path
        pages = 0
        while url:
            pages += 1
            if pages > 50:
                raise GitHubError(0, "pagination limit")
            data, url = self._request("GET", url)
            if not isinstance(data, list):
                raise GitHubError(0, "expected a list")
            items.extend(data)
        return items

    def label_exists(self, repo: str, name: str) -> bool:
        quoted = urllib.parse.quote(name, safe="")
        try:
            self._request("GET", f"/repos/{repo}/labels/{quoted}")
        except GitHubError as exc:
            if exc.status == 404:
                return False
            raise
        return True

    def create_label(self, repo: str, name: str, description: str, color: str) -> None:
        self._request(
            "POST",
            f"/repos/{repo}/labels",
            {"name": name, "description": description, "color": color},
        )

    def list_labeled_issues(self, repo: str, label: str, state: str) -> list[dict]:
        query = urllib.parse.urlencode({"state": state, "labels": label, "per_page": 100})
        items = self._get_pages(f"/repos/{repo}/issues?{query}")
        return [item for item in items if "pull_request" not in item]

    def list_releases(self, repo: str) -> list[dict]:
        return self._get_pages(f"/repos/{repo}/releases?per_page=100")

    def get_issue(self, repo: str, number: int) -> dict | None:
        try:
            data, _ = self._request("GET", f"/repos/{repo}/issues/{number}")
        except GitHubError as exc:
            if exc.status == 404:
                return None
            raise
        return data

    def list_comments(self, repo: str, number: int) -> list[dict]:
        return self._get_pages(f"/repos/{repo}/issues/{number}/comments?per_page=100")

    def post_comment(self, repo: str, number: int, body: str) -> None:
        self._request("POST", f"/repos/{repo}/issues/{number}/comments", {"body": body})

    def set_issue_state(self, repo: str, number: int, state: str, state_reason: str | None = None) -> None:
        payload: dict[str, str] = {"state": state}
        if state_reason:
            payload["state_reason"] = state_reason
        self._request("PATCH", f"/repos/{repo}/issues/{number}", payload)

    def list_run_jobs(self, repo: str, run_id: str) -> list[dict]:
        jobs: list[dict] = []
        url: str | None = f"/repos/{repo}/actions/runs/{run_id}/jobs?per_page=100"
        pages = 0
        while url:
            pages += 1
            if pages > 20:
                raise GitHubError(0, "job pagination limit")
            data, url = self._request("GET", url)
            jobs.extend(data.get("jobs") or [])
        return jobs

    def find_tracking_issues(self, repo: str) -> list[dict]:
        found: dict[int, dict] = {}
        query = urllib.parse.quote(f"repo:{repo} mirror-public-reply-audit in:body")
        data, _ = self._request("GET", f"/search/issues?q={query}&per_page=20")
        for item in data.get("items") or []:
            if "pull_request" in item:
                continue
            if AUDIT_MARKER in (item.get("body") or ""):
                found[int(item["number"])] = item
        # Search is briefly stale after a create. The newest issues cover that gap.
        recent, _ = self._request(
            "GET",
            f"/repos/{repo}/issues?state=all&sort=created&direction=desc&per_page=30",
        )
        for item in recent or []:
            if "pull_request" in item:
                continue
            if AUDIT_MARKER in (item.get("body") or ""):
                found[int(item["number"])] = item
        return list(found.values())

    def create_issue(self, repo: str, title: str, body: str) -> dict:
        data, _ = self._request("POST", f"/repos/{repo}/issues", {"title": title, "body": body})
        return data

    def update_issue(self, repo: str, number: int, fields: dict) -> None:
        self._request("PATCH", f"/repos/{repo}/issues/{number}", fields)


def ensure_label(github: GitHub, repo: str, dry_run: bool) -> None:
    if github.label_exists(repo, LABEL):
        return
    if dry_run:
        print(f"dry-run would create label {LABEL} on {repo}")
        return
    github.create_label(repo, LABEL, LABEL_DESCRIPTION, LABEL_COLOR)
    print(f"created label {LABEL} on {repo}")


def _public_refused(exc: GitHubError) -> bool:
    return exc.status in (401, 403)


def _load_public(public: GitHub, public_repo: str, number: int) -> PublicView:
    issue = public.get_issue(public_repo, number)
    comments = [] if issue is None else public.list_comments(public_repo, number)
    if issue is not None and "pull_request" in issue:
        return PublicView(number=number, state=None, comments=(), missing=True)
    return public_view(number, issue, comments)


def wait_for_push(private: GitHub, cfg: CloseConfig, sleep=time.sleep) -> bool:
    """True when this mirror run recorded a push. False when it finished without one."""
    last_error: GitHubError | None = None
    for attempt in range(cfg.mirror_attempts):
        try:
            jobs = private.list_run_jobs(cfg.private_repo, cfg.mirror_run_id)
        except GitHubError as exc:
            last_error = exc
            jobs = []
        if mirror_pushed(jobs):
            return True
        if mirror_run_settled(jobs):
            return False
        if attempt + 1 < cfg.mirror_attempts:
            sleep(cfg.mirror_pause)
    if last_error is not None and not mirror_run_settled([]):
        raise last_error
    raise GitHubError(0, "mirror run jobs were not available")


def run_close(private: GitHub, public: GitHub, cfg: CloseConfig, sleep=time.sleep) -> int:
    if cfg.mirror_run_id:
        try:
            pushed = wait_for_push(private, cfg, sleep=sleep)
        except GitHubError as exc:
            print(f"could not read the mirror run: HTTP {exc.status}", file=sys.stderr)
            return 1
        if not pushed:
            print("mirror run did not push; no public comments")
            return 0
    if not cfg.public_repo:
        print("PUBLIC_REPO is not set; no public comments")
        return 0
    forced = cfg.forced_version or None
    if forced and not is_version(forced):
        print(f"refusing a public comment for version {forced!r}", file=sys.stderr)
        return 1

    try:
        ensure_label(private, cfg.private_repo, cfg.dry_run)
        raw_issues = private.list_labeled_issues(cfg.private_repo, LABEL, "closed")
        raw_releases = private.list_releases(cfg.private_repo)
    except GitHubError as exc:
        print(f"could not read source issues: HTTP {exc.status}", file=sys.stderr)
        return 1

    issues = [private_issue_from_api(item) for item in raw_issues]
    releases: list[Release] = []
    for item in raw_releases:
        rel = release_from_api(item)
        if rel is not None:
            releases.append(rel)
    releases = with_release(releases, cfg.release_tag, cfg.release_published_at)

    actions: list[CloseAction] = []
    for issue in issues:
        try:
            chosen = consider_close(issue, releases, cfg.public_repo, forced)
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            return 1
        if chosen is None:
            continue
        number, version = chosen
        try:
            view = _load_public(public, cfg.public_repo, number)
        except GitHubError as exc:
            if _public_refused(exc):
                print(
                    "MIRROR_PUSH_TOKEN was refused by the public repository "
                    "(needs Issues: Read and write).",
                    file=sys.stderr,
                )
                return 1
            print(f"{cfg.public_repo}#{number} failed: HTTP {exc.status}", file=sys.stderr)
            actions.append(CloseAction(issue.number, number, version, False, False, "public read failed"))
            continue
        action = action_for(issue, number, version, view)
        if action is not None:
            actions.append(action)

    deduped: list[CloseAction] = []
    seen: set[int] = set()
    for action in actions:
        if action.public_number in seen:
            continue
        seen.add(action.public_number)
        deduped.append(action)
    actions = deduped

    writes = [action for action in actions if action.post_comment or action.close]
    if writes and not cfg.dry_run and not public.token:
        print(
            "MIRROR_PUSH_TOKEN is missing; refusing to comment on the public repository",
            file=sys.stderr,
        )
        return 1

    failures = 0
    for action in actions:
        line = format_close_line(cfg.public_repo, action)
        if action.error:
            print(line, file=sys.stderr)
            failures += 1
            continue
        if cfg.dry_run:
            print("dry-run " + line)
            continue
        try:
            if action.post_comment:
                public.post_comment(cfg.public_repo, action.public_number, comment_for(action.version))
            if action.close:
                public.set_issue_state(cfg.public_repo, action.public_number, "closed", "completed")
        except GitHubError as exc:
            if _public_refused(exc):
                print(
                    "MIRROR_PUSH_TOKEN was refused by the public repository "
                    "(needs Issues: Read and write).",
                    file=sys.stderr,
                )
                return 1
            print(f"{cfg.public_repo}#{action.public_number} failed: HTTP {exc.status}", file=sys.stderr)
            failures += 1
            continue
        print(line)
    if not actions:
        print("no public issues to comment on")
    return 1 if failures else 0


def _tracking_fields(existing: dict | None, body: str, has_rows: bool) -> dict | None:
    """None when nothing should be written. ``{"create": True, ...}`` to open one."""
    want_state = "open" if has_rows else "closed"
    if existing is None:
        if not has_rows:
            return None
        return {"create": True, "title": AUDIT_TITLE, "body": body}
    same_body = (existing.get("body") or "").strip() == body.strip()
    same_state = existing.get("state") == want_state
    if same_body and same_state:
        return {}
    fields: dict[str, str] = {}
    if not same_body:
        fields["body"] = body
    if not same_state:
        fields["state"] = want_state
        if want_state == "closed":
            fields["state_reason"] = "completed"
    return fields


def run_audit(private: GitHub, public: GitHub, cfg: AuditConfig) -> int:
    if not cfg.public_repo:
        print("PUBLIC_REPO is not set; no audit")
        return 0
    try:
        ensure_label(private, cfg.private_repo, cfg.dry_run)
        raw_issues = private.list_labeled_issues(cfg.private_repo, LABEL, "closed")
    except GitHubError as exc:
        print(f"could not read source issues: HTTP {exc.status}", file=sys.stderr)
        return 1

    issues = [private_issue_from_api(item) for item in raw_issues]
    public_by_number: dict[int, PublicView] = {}
    for issue in issues:
        marker = parse_marker(issue.body, cfg.public_repo)
        if marker.error or marker.number is None or marker.number in public_by_number:
            continue
        try:
            public_by_number[marker.number] = _load_public(public, cfg.public_repo, marker.number)
        except GitHubError as exc:
            if _public_refused(exc):
                print(
                    "MIRROR_PUSH_TOKEN was refused by the public repository "
                    "(needs Issues: Read and write).",
                    file=sys.stderr,
                )
                return 1
            print(f"{cfg.public_repo}#{marker.number} failed: HTTP {exc.status}", file=sys.stderr)
            return 1

    rows = plan_audit(issues, public_by_number, cfg.public_repo)
    body = render_audit(rows, cfg.public_repo, cfg.checked_at)
    try:
        existing_list = private.find_tracking_issues(cfg.private_repo)
    except GitHubError as exc:
        print(f"could not read the tracking issue: HTTP {exc.status}", file=sys.stderr)
        return 1
    existing = min(existing_list, key=lambda item: int(item["number"])) if existing_list else None
    fields = _tracking_fields(existing, body, bool(rows))

    if fields is None:
        print("No from-public closes are missing a public reply.")
        return 0
    if cfg.dry_run:
        if fields.get("create"):
            print("dry-run would open the tracking issue")
        elif not fields:
            print(f"dry-run tracking issue #{existing['number']} already current")
        else:
            print(f"dry-run would update tracking issue #{existing['number']}")
        print(body)
        return 0
    try:
        if fields.get("create"):
            made = private.create_issue(cfg.private_repo, AUDIT_TITLE, body)
            print(f"opened tracking issue #{made['number']}")
            return 0
        if not fields:
            print(f"tracking issue #{existing['number']} already current")
            return 0
        private.update_issue(cfg.private_repo, int(existing["number"]), fields)
        print(f"updated tracking issue #{existing['number']}")
    except GitHubError as exc:
        print(f"could not write the tracking issue: HTTP {exc.status}", file=sys.stderr)
        return 1
    return 0


def dry_run_enabled(arg_dry: bool, env: str | None) -> bool:
    return arg_dry or env == "1"


def _env(name: str) -> str:
    return (os.environ.get(name) or "").strip()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Public mirror issue closure and reply audit")
    sub = parser.add_subparsers(dest="command", required=True)
    close = sub.add_parser("close")
    close.add_argument("--dry-run", action="store_true")
    close.add_argument("--version", default="")
    audit = sub.add_parser("audit")
    audit.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    private_repo = _env("PRIVATE_REPO") or _env("GITHUB_REPOSITORY")
    public_repo = _env("PUBLIC_REPO")
    if not private_repo:
        print("PRIVATE_REPO or GITHUB_REPOSITORY is required", file=sys.stderr)
        return 1
    private = GitHub(_env("GITHUB_TOKEN"))
    public = GitHub(_env("MIRROR_PUSH_TOKEN"))
    dry = dry_run_enabled(args.dry_run, os.environ.get("DRY_RUN"))

    if args.command == "close":
        forced = (args.version or _env("FORCE_VERSION")).strip()
        cfg = CloseConfig(
            private_repo=private_repo,
            public_repo=public_repo,
            dry_run=dry,
            forced_version=forced,
            release_tag=_env("RELEASE_TAG"),
            release_published_at=_env("RELEASE_PUBLISHED_AT"),
            mirror_run_id=_env("MIRROR_RUN_ID"),
        )
        return run_close(private, public, cfg)

    checked = datetime.now(UTC).strftime("%Y-%m-%dT%H:%MZ")
    cfg = AuditConfig(
        private_repo=private_repo,
        public_repo=public_repo,
        dry_run=dry,
        checked_at=checked,
    )
    return run_audit(private, public, cfg)


if __name__ == "__main__":
    sys.exit(main())
