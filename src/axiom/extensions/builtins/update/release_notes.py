# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Fetch and format what changed, for someone deciding whether to upgrade.

"An update is available" is not a reason to take one. The question being asked
is whether the new version fixes something that is biting them, and answering
it means showing what changed in words they can read.

The notes come from the release being offered, not from the installed package:
a wheel cannot carry notes about its own successor.

Everything here is best-effort and silent on failure. It runs on a CLI startup
path, and a shell that will not start because a changelog fetch timed out is a
worse outcome than an unannounced update.
"""

from __future__ import annotations

import json
import re

#: Bodies that carry no information. Every release cut before the workflow
#: began generating notes has exactly this shape, and showing it is worse than
#: showing nothing: it occupies the place a changelog should be.
_PLACEHOLDER = re.compile(r"^\s*automated release\b.*$", re.IGNORECASE | re.DOTALL)

#: Enough to judge an upgrade by; past this the terminal is being flooded.
_MAX_LINES = 12


def fetch_release_notes(repo: str, version: str, *, timeout: float = 4.0) -> str:
    """Release notes for ``version`` from ``repo`` (``owner/name``), or "".

    Never raises.
    """
    if not repo or not version:
        return ""
    try:
        import urllib.request

        url = f"https://api.github.com/repos/{repo}/releases/tags/v{version}"
        req = urllib.request.Request(  # noqa: S310 - fixed https host
            url, headers={"Accept": "application/vnd.github+json"}
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
            body = (json.loads(resp.read()) or {}).get("body") or ""
    except Exception:  # noqa: BLE001 - offline, rate limited, no such release
        return ""

    body = body.strip()
    if not body or _PLACEHOLDER.match(body):
        return ""
    return body


def _readable(notes: str) -> list[str]:
    """Markdown release notes as plain lines fit for a terminal."""
    lines: list[str] = []
    for raw in notes.splitlines():
        line = raw.strip()
        if not line or line.startswith("**Full Changelog**"):
            continue
        if line.startswith("#"):  # section headings read as clutter here
            continue
        line = re.sub(r"^[*\-]\s+", "• ", line)
        # One clause, removed as one. Taking the author and the number out
        # separately leaves a dangling "in" at the end of every line.
        line = re.sub(
            r"\s+by\s+@[\w-]+(?:\s+in\s+(?:#\d+|https?://\S+))?", "", line
        )
        line = re.sub(r"\s+in\s+https?://\S+", "", line)   # bare PR links
        line = re.sub(r"\s+#\d+\b", "", line)              # bare PR numbers
        line = re.sub(r"[*_`]+", "", line)                 # emphasis marks
        if line.strip(" •"):
            lines.append(line)
    return lines


def format_update_notice(
    *, product: str, current: str, available: str, notes: str
) -> str:
    """The message a person reads when asked whether to upgrade."""
    header = f"{product} {available} is available — you are on {current}."
    lines = _readable(notes)
    if not lines:
        # No notes is not a reason to say nothing: the versions alone still
        # answer "is this worth doing".
        return header

    shown = lines[:_MAX_LINES]
    body = "\n".join(f"  {line}" for line in shown)
    if len(lines) > len(shown):
        body += f"\n  …and {len(lines) - len(shown)} more changes"
    return f"{header}\n\nWhat's new:\n{body}"


__all__ = ["fetch_release_notes", "format_update_notice"]
