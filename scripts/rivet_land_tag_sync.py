#!/usr/bin/env python3
# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0
"""Keep the public `rivet-land-v1` tag on the mirror's current commit.

Every repo that runs RIVET calls `actions/rivet-land@rivet-land-v1`. That tag is the
only thing between a release and those repos, so it must move with the mirror. It moves
only when the axiom-os-lm release the action pins (`axiom-version` in action.yml) is
already on PyPI; a tag on a pin nobody can install would break RIVET everywhere.

    rivet_land_tag_sync.py <action.yml> <owner/repo> <commit-sha>   (GH_TOKEN in the env)

Exit 0 when the tag is current or was moved, or when the pin is not published yet (a
warning, and the tag stays where it is). Exit 1 on an API error, so it surfaces.
"""

from __future__ import annotations

import json
import os
import re
import sys
import urllib.error
import urllib.request

TAG = "rivet-land-v1"
PIN = re.compile(r"^  axiom-version:\n(?:    .*\n)*?    default:\s*[\"']?([0-9][^\"'\s]*)", re.M)


def pinned_version(action_yml: str) -> str | None:
    m = PIN.search(action_yml)
    return m.group(1) if m else None


def _call(url: str, token: str | None = None, method: str = "GET", body: dict | None = None):
    req = urllib.request.Request(url, method=method)
    req.add_header("Accept", "application/vnd.github+json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    data = json.dumps(body).encode() if body is not None else None
    with urllib.request.urlopen(req, data, timeout=30) as r:
        return json.loads(r.read() or b"null")


def published(version: str) -> bool:
    try:
        _call(f"https://pypi.org/pypi/axiom-os-lm/{version}/json")
        return True
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return False
        raise


def sync(action_yml: str, repo: str, sha: str, token: str) -> str:
    version = pinned_version(action_yml)
    if not version:
        raise SystemExit("::error::no axiom-version default found in action.yml")
    if not published(version):
        return f"::warning::{TAG} stays: axiom-os-lm {version} is not on PyPI yet"
    api = f"https://api.github.com/repos/{repo}/git/refs"
    try:
        current = _call(f"{api}/tags/{TAG}", token)["object"]["sha"]
    except urllib.error.HTTPError as exc:
        if exc.code != 404:
            raise
        _call(api, token, "POST", {"ref": f"refs/tags/{TAG}", "sha": sha})
        return f"{TAG} created at {sha[:9]} (pins axiom-os-lm {version})"
    if current == sha:
        return f"{TAG} already at {sha[:9]} (pins axiom-os-lm {version})"
    _call(f"{api}/tags/{TAG}", token, "PATCH", {"sha": sha, "force": True})
    return f"{TAG} moved {current[:9]} -> {sha[:9]} (pins axiom-os-lm {version})"


def main(argv: list[str]) -> int:
    path, repo, sha = argv[1:4]
    with open(path, encoding="utf-8") as fh:
        text = fh.read()
    try:
        print(sync(text, repo, sha, os.environ["GH_TOKEN"]))
    except urllib.error.HTTPError as exc:
        print(f"::error::{TAG}: {exc.code} {exc.reason} from {exc.url}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
