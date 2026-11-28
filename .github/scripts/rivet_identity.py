#!/usr/bin/env python3
# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Say it when RIVET has no app identity, and file one incident when the red-main watcher is down.

The RIVET app token could not be minted from 2026-11-22. Auto-revert crashed quietly, main stayed
red for hours with no incident, and the lander ran without its identity behind a green step (#2341).

Modes:
  notice    print "running without the RIVET identity: <reason>" when the token step failed,
            and nothing when it did not. Reads RIVET_APP_TOKEN_OUTCOME, RIVET_APP_ID and
            RIVET_APP_KEY_SET (true or false, from ``secrets.RIVET_APP_PRIVATE_KEY != ''``).
  incident  find or open ONE issue titled TITLE and comment on it each time the watcher fails.
            Uses only GH_TOKEN (the job token): it needs no app key. Reads GITHUB_REPOSITORY,
            RIVET_FAILURE_REASON and RIVET_RUN_URL.

Standard library only, so it runs before anything is installed. It never reads or prints the key.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys

TITLE = "The red-main watcher is down: RIVET cannot mint its app token"
_ENV_SECRETS = ("GH_TOKEN", "GITHUB_TOKEN", "RIVET_CI_START_TOKEN")


def notice(environ: dict[str, str] | None = None) -> str:
    """One plain sentence when the token step failed; an empty string otherwise."""
    env = os.environ if environ is None else environ
    if env.get("RIVET_APP_TOKEN_OUTCOME", "") != "failure":
        return ""
    if not env.get("RIVET_APP_ID", "").strip():
        reason = "RIVET_APP_ID is not set"
    elif env.get("RIVET_APP_KEY_SET", "") == "false":
        reason = "RIVET_APP_PRIVATE_KEY is not set"
    else:
        reason = (
            "the app token could not be minted (the 'RIVET app token' step failed; "
            "its log has the forge's own error)"
        )
    return (
        f"running without the RIVET identity: {reason}. CI that never ran cannot be started "
        "and nothing can be reverted until it is fixed."
    )


def _redact(text: str) -> str:
    for name in _ENV_SECRETS:
        secret = os.environ.get(name) or ""
        if secret:
            text = text.replace(secret, "[redacted]")
    return text


def _gh(args: list[str]) -> str:
    done = subprocess.run(["gh", *args], capture_output=True, text=True, check=False)
    if done.returncode != 0:
        raise SystemExit(_redact((done.stderr or done.stdout or "gh failed").strip()))
    return done.stdout


def _body(reason: str, run_url: str) -> str:
    return (
        "The workflow that watches main and reverts a commit that turned it red could not mint "
        "the RIVET app token, so it did nothing for this failure.\n\n"
        f"- Cause: {reason}\n"
        f"- Run: {run_url or 'unknown'}\n\n"
        "While this is open, main can stay red with no incident and no revert, and the lander "
        "cannot start CI that never ran.\n\n"
        "To fix it, check the repository variable RIVET_APP_ID and the secret "
        "RIVET_APP_PRIVATE_KEY (the file must keep its newlines), then re-run the "
        "`Loop auto-revert` workflow. Close this issue when a run mints the token."
    )


def file_incident(repo: str, reason: str, run_url: str, *, gh=_gh) -> str:
    """Open the one incident, or comment on it when it is already open. Returns what was done."""
    body = _body(_redact(reason), run_url)
    found = json.loads(
        gh(
            ["issue", "list", "--repo", repo, "--state", "open", "--search", f'"{TITLE}" in:title',
             "--json", "number,title", "--limit", "20"]
        )
        or "[]"
    )
    existing = [row for row in found if row.get("title") == TITLE]
    if existing:
        number = str(existing[0]["number"])
        gh(["issue", "comment", number, "--repo", repo, "--body", body])
        return f"commented on #{number}"
    gh(["issue", "create", "--repo", repo, "--title", TITLE, "--body", body])
    return "opened the incident"


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    mode = args[0] if args else ""
    if mode == "notice":
        line = notice()
        if line:
            print(line)
        return 0
    if mode == "incident":
        repo = os.environ.get("GITHUB_REPOSITORY", "").strip()
        if not repo:
            print("GITHUB_REPOSITORY is required", file=sys.stderr)
            return 1
        reason = os.environ.get("RIVET_FAILURE_REASON", "").strip() or "the app token could not be minted"
        print(file_incident(repo, reason, os.environ.get("RIVET_RUN_URL", "")))
        return 0
    print("usage: rivet_identity.py notice|incident", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
