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
        # Emphasis marks at word edges only: an underscore inside an
        # identifier (snake_case names) is part of the name.
        line = re.sub(r"(?<![\w])[*_`]+|[*_`]+(?![\w])", "", line)
        if re.match(r"^•?\s*bump:", line):              # version bumps carry no news
            continue
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


# -- every release being taken, from the official release notes ----------------

#: Per release, and in all, so a long gap stays readable; the link carries the rest.
_PER_RELEASE = 4
_TOTAL = 24


def repo_from_project_urls(urls: list[str] | None) -> str:
    """``owner/name`` from a package's own ``Project-URL`` entries, or ""."""
    for entry in urls or []:
        _, _, url = entry.partition(",")
        m = re.search(r"github\.com/([\w.-]+)/([\w.-]+?)(?:\.git)?/?$", url.strip())
        if m:
            return f"{m.group(1)}/{m.group(2)}"
    return ""


def package_repo(package: str) -> str:
    """The repository a distribution names for itself."""
    try:
        import importlib.metadata as md

        return repo_from_project_urls(md.metadata(package).get_all("Project-URL"))
    except Exception:  # noqa: BLE001 - no metadata: no repository known
        return ""


def _github_token() -> str | None:
    """The person's own GitHub login, if they have one: env first, then gh."""
    import os
    import subprocess

    for var in ("GH_TOKEN", "GITHUB_TOKEN"):
        if os.environ.get(var):
            return os.environ[var]
    try:
        out = subprocess.run(["gh", "auth", "token"], capture_output=True, text=True, timeout=3)
        return out.stdout.strip() or None
    except Exception:  # noqa: BLE001 - gh absent or signed out
        return None


def _get_json(url: str, token: str | None):
    import urllib.request

    headers = {"Accept": "application/vnd.github+json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, headers=headers)  # noqa: S310 - fixed https host
    with urllib.request.urlopen(req, timeout=4.0) as resp:  # noqa: S310
        return json.loads(resp.read())


def fetch_releases(repo: str, *, getter=None, token_source=None) -> list[dict]:
    """The official releases of ``repo``, newest first, or [] when unreadable.

    Anonymous first; a private repository is then read with the person's own
    GitHub login, never a shared one. Never raises.
    """
    if not repo:
        return []
    get = getter or _get_json
    url = f"https://api.github.com/repos/{repo}/releases?per_page=100"
    try:
        return list(get(url, None) or [])
    except Exception:  # noqa: BLE001 - private, offline or rate limited
        pass
    token = (token_source or _github_token)()
    if not token:
        return []
    try:
        return list(get(url, token) or [])
    except Exception:  # noqa: BLE001
        return []


def releases_between(releases: list[dict], *, current: str, available: str) -> list[dict]:
    """Final releases newer than ``current`` up to ``available``, newest first."""
    from packaging.version import InvalidVersion, Version

    try:
        lo, hi = Version(current), Version(available)
    except InvalidVersion:
        return []
    out = []
    for r in releases:
        tag = str(r.get("tag_name") or "").lstrip("v")
        try:
            v = Version(tag)
        except InvalidVersion:
            continue
        if v.is_prerelease or not (lo < v <= hi):
            continue
        body = str(r.get("body") or "").strip()
        out.append({"version": tag, "notes": "" if _PLACEHOLDER.match(body) else body, "url": r.get("html_url") or ""})
    return sorted(out, key=lambda r: Version(r["version"]), reverse=True)


def format_update_notice_for(
    *, product: str, current: str, available: str, releases: list[dict], repo: str
) -> str:
    """What someone reads before answering: each release being taken, and where the rest is."""
    count = len(releases)
    behind = f" ({count} releases)" if count > 1 else ""
    lines = [f"{product} {available} is available{behind}; you are on {current}."]
    link = f"https://github.com/{repo}/releases" if repo else ""
    shown = hidden = hidden_releases = 0
    for r in releases:
        changes = _readable(r["notes"])
        if not changes:
            continue
        if shown >= _TOTAL:
            hidden += len(changes)
            hidden_releases += 1
            continue
        lines += ["", f"{r['version']}:"]
        room = min(_PER_RELEASE, _TOTAL - shown)
        lines += [f"  {c}" for c in changes[:room]]
        shown += min(room, len(changes))
        hidden += max(0, len(changes) - room)
    if hidden:
        more = f" in {hidden_releases} more releases" if hidden_releases else ""
        lines += ["", f"  …and {hidden} more changes{more}" + (f"; all notes: {link}" if link else "")]
    elif link and (shown == 0 or count > 1):
        lines += ["", f"Release notes: {link}"]
    return "\n".join(lines)


__all__ = [
    "fetch_release_notes",
    "fetch_releases",
    "format_update_notice",
    "format_update_notice_for",
    "package_repo",
    "releases_between",
    "repo_from_project_urls",
]
