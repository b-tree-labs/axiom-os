# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Copy new public issues into the private tracker. See public_issue_intake.py."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from public_issue_intake import classify  # noqa: E402

PUBLIC = os.environ["PUBLIC_REPO"]
PRIVATE = os.environ["PRIVATE_REPO"]
SINCE = os.environ["SINCE"]

LABELS = {
    "from-public": "5319e7",
    "triage:ready": "0e8a16",
    "needs-review": "fbca04",
    "safety-refused": "b60205",
}


def _gh(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["gh", *args], capture_output=True, text=True, check=check,
    )


def _ensure_labels() -> None:
    for name, color in LABELS.items():
        _gh("label", "create", name, "--repo", PRIVATE, "--color", color,
            "--force", check=False)


def _already_copied(url: str) -> bool:
    found = _gh(
        "issue", "list", "--repo", PRIVATE, "--state", "all",
        "--search", f'"{url}" in:body', "--limit", "5", "--json", "number,body",
        check=False,
    )
    try:
        rows = json.loads(found.stdout or "[]")
    except json.JSONDecodeError:
        return False
    return any(url in (row.get("body") or "") for row in rows)


def main() -> int:
    _ensure_labels()
    items = json.loads(Path("/tmp/public.json").read_text(encoding="utf-8"))
    for it in items:
        if (it.get("createdAt") or "") < SINCE:
            continue
        number = it["number"]
        title = it.get("title") or ""
        body = it.get("body") or ""
        url = it["url"]
        if _already_copied(url):
            continue
        verdict = classify(title, body)
        action = verdict["action"]
        origin = f"Public-Origin: {url}"
        if action == "refuse":
            private_body = (
                f"{origin}\n\n"
                f"Safety review: REFUSED. {verdict['reason']}.\n"
                "The request was not copied and is not scheduled.\n"
            )
            labels = "from-public,safety-refused"
        elif action == "review":
            private_body = (
                f"{origin}\n\n"
                f"Safety review: needs a person. {verdict['reason']}.\n"
                "Not scheduled until someone reads the public issue.\n"
            )
            labels = "from-public,needs-review"
        else:
            private_body = (
                f"{origin}\n\n"
                f"Safety review: accepted. {verdict['reason']}.\n\n"
                "Scheduled for work.\n\n---\n\n" + body
            )
            labels = "from-public,triage:ready"
        _gh(
            "issue", "create", "--repo", PRIVATE,
            "--title", f"[public #{number}] {title}",
            "--body", private_body, "--label", labels,
        )
        if action == "refuse":
            _gh(
                "issue", "comment", str(number), "--repo", PUBLIC,
                "--body",
                "This was not accepted as work. It reads as a request to "
                "weaken a control or carry out an attack. Vulnerability "
                "reports go to SECURITY.md and are not implemented from a "
                "public issue.",
                check=False,
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
