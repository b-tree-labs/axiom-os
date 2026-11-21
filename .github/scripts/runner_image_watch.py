#!/usr/bin/env python3
"""Watch actions/runner-images announcements for runner moves that need a re-pin.

Flags open issues labeled "Announcement" in actions/runner-images that either
move the ``ubuntu-latest`` label or deprecate / brown out / retire Ubuntu 24.04
(the image our workflows pin). Keeps ONE tracking issue in this repo, found by a
hidden marker: created when something matches, its body updated when the matches
change, left alone otherwise. No match -> no-op.

Env: GITHUB_TOKEN, GITHUB_REPOSITORY (owner/repo). DRY_RUN=1 prints, writes nothing.
See docs/runbooks/repin-policy.md.
"""

from __future__ import annotations

import json
import os
import re
import sys
import urllib.request

API = "https://api.github.com"
MARKER = "<!-- runner-image-watch -->"
TITLE = "CI: GitHub runner image change needs a re-pin decision"
UPSTREAM = "actions/runner-images"

LATEST_MOVE = re.compile(r"ubuntu-latest", re.I)
PINNED_EOL = re.compile(r"24\.04.*(deprecat|brownout|unsupported|remov|retir)", re.I)


def _req(method: str, path: str, body: dict | None = None):
    url = path if path.startswith("http") else f"{API}{path}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("User-Agent", "runner-image-watch")
    token = os.getenv("GITHUB_TOKEN")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    if data is not None:
        req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.load(resp)


def find_matches() -> list[dict]:
    issues = _req("GET", f"/repos/{UPSTREAM}/issues?labels=Announcement&state=open&per_page=100")
    hits = []
    for i in issues:
        if "pull_request" in i:
            continue
        title = i["title"]
        if not title.lower().startswith("[ubuntu"):
            continue
        if LATEST_MOVE.search(title) or PINNED_EOL.search(title):
            hits.append({"number": i["number"], "title": title, "url": i["html_url"]})
    return sorted(hits, key=lambda h: h["number"])


def render(hits: list[dict]) -> str:
    lines = [
        MARKER,
        "GitHub announced a runner-image change that affects our pins "
        "(`runs-on: ubuntu-24.04`). Follow docs/runbooks/repin-policy.md: trial branch "
        "on the new image, full test matrix once, promote only after green.",
        "",
        "Matching `actions/runner-images` announcements:",
    ]
    lines += [f"- [#{h['number']}]({h['url']}) {h['title']}" for h in hits]
    lines += [
        "",
        "_Maintained by `.github/workflows/runner-image-watch.yml`; close once re-pinned._",
    ]
    return "\n".join(lines)


def main() -> int:
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    dry = os.getenv("DRY_RUN") == "1"
    hits = find_matches()
    if not hits:
        print("No ubuntu-latest move or 24.04 deprecation announced. No-op.")
        return 0
    body = render(hits)
    print(body)

    existing = None
    if repo:
        for i in _req("GET", f"/repos/{repo}/issues?state=open&per_page=100"):
            if MARKER in (i.get("body") or ""):
                existing = i
                break

    if dry or not repo:
        print(
            f"\nDRY RUN: would {'update #' + str(existing['number']) if existing else 'open a new issue'}."
        )
        return 0
    if existing is None:
        made = _req("POST", f"/repos/{repo}/issues", {"title": TITLE, "body": body})
        print(f"Opened tracking issue #{made['number']}")
    elif (existing.get("body") or "").strip() != body.strip():
        _req("PATCH", f"/repos/{repo}/issues/{existing['number']}", {"body": body})
        print(f"Updated tracking issue #{existing['number']}")
    else:
        print(f"Tracking issue #{existing['number']} already current. No-op.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
