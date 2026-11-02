#!/usr/bin/env python3
# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Post or update one warn-only RIVET report on the current pull request.

The report is what the lander would do. This script does not merge, enqueue,
or push. An existing comment with MARKER is updated, so a new push replaces
the previous report instead of adding another.

Env: GH_TOKEN, GITHUB_REPOSITORY (owner/name), PR_NUMBER, RIVET_REPORT_PATH.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

MARKER = "<!-- rivet-lander:warn-only -->"


def comment_body(report: str) -> str:
    """One comment: the warn-only report, fenced so the body stays literal."""
    text = report.strip()
    fence = "````"
    while fence in text:
        fence += "`"
    return (
        f"{MARKER}\n"
        "**RIVET (live).** This comment is the landing decision. "
        "A green, unprotected, non-draft pull request with no pending "
        "human review and no changes-requested review may land. "
        "A protected path is held and is not landed. "
        "A pending human review or a changes-requested review waits.\n\n"
        f"{fence}\n{text}\n{fence}\n"
    )


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


def existing_comment(repo: str, number: str) -> str:
    """The first comment id that already carries MARKER, or an empty string."""
    needle = json.dumps(MARKER)
    raw = _gh(
        [
            "api",
            "--paginate",
            f"repos/{repo}/issues/{number}/comments",
            "--jq",
            f".[] | select(.body | contains({needle})) | .id",
        ]
    )
    for line in raw.splitlines():
        token = line.strip()
        if token:
            return token
    return ""


def publish(repo: str, number: str, body: str) -> None:
    comment_id = existing_comment(repo, number)
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


def main() -> int:
    repo = os.environ.get("GITHUB_REPOSITORY", "").strip()
    number = os.environ.get("PR_NUMBER", "").strip()
    path = os.environ.get("RIVET_REPORT_PATH", "").strip()
    if not repo or not number or not path:
        print("GITHUB_REPOSITORY, PR_NUMBER, and RIVET_REPORT_PATH are required", file=sys.stderr)
        return 1
    body = comment_body(Path(path).read_text(encoding="utf-8"))
    publish(repo, number, body)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
