# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Read-only tracker clients for the capture feeders.

Two small clients, one protocol. Each is **read-only** (the feeders ingest,
attribute, and detect; nothing in this phase writes to a tracker), resolves
its credential from the Axiom vault at construction and **never prints or
logs it**, and is exception-safe: an unreachable host or a bad response
degrades to "unverified" / empty, never a crash.

The protocol :class:`TrackerClient` is what the sources depend on, so a unit
test injects a fake and no live network is touched. The concrete
:class:`GitLabClient` / :class:`GitHubCliClient` are the production readers;
they are deliberately thin and are exercised by integration, not unit tests.

Credential resolution goes through the foreign-credential store (what
``axi secrets get`` reads): the deployment names the vault entry in config
(``program.tracker.credential``), the client resolves it by name, and the
plaintext lives only inside the client instance.
"""

from __future__ import annotations

import json
from typing import Any, Protocol, runtime_checkable

from ._capture import MergeRequest, TrackerIssue

# ---------------------------------------------------------------------------
# Credential resolution (vault, never argv / env / logs)
# ---------------------------------------------------------------------------


class CredentialUnavailable(RuntimeError):
    """The named vault credential is not present or could not be read."""


def resolve_vault_token(name: str, state_dir: Any) -> str:
    """Read a foreign-credential value from the vault by name.

    Raises :class:`CredentialUnavailable` rather than returning an empty
    string, so a caller distinguishes "no credential configured" from "an
    empty token". The value is returned to the caller and never logged here.
    """
    from axiom.extensions.builtins.secrets.foreign.store import ForeignCredentialStore

    if not name:
        raise CredentialUnavailable("no credential name configured")
    store = ForeignCredentialStore(state_dir)
    if not store.exists(name):
        raise CredentialUnavailable(f"no vault credential named {name!r}")
    try:
        with store.get(name) as secret:
            return secret.as_str()
    except Exception as exc:  # noqa: BLE001 — a read failure is "unavailable", not a crash
        raise CredentialUnavailable(f"could not read credential {name!r}") from exc


# ---------------------------------------------------------------------------
# The protocol the sources depend on
# ---------------------------------------------------------------------------


@runtime_checkable
class TrackerClient(Protocol):
    """A read-only tracker reader. Every method is exception-safe."""

    def ping(self) -> bool:
        """Is the host reachable at all?"""
        ...

    def whoami(self) -> str | None:
        """The authenticated account username, or ``None`` if auth fails."""
        ...

    def project_readable(self) -> bool:
        """Can the configured project be read with this credential?"""
        ...

    def issues(self, since: str | None) -> list[TrackerIssue]:
        """Issues updated after ``since`` (the cursor), normalized."""
        ...

    def merge_requests(self, since: str | None) -> list[MergeRequest]:
        """Merge/pull requests updated after ``since``, normalized."""
        ...

    def commits(self, host: str, repo: str) -> set[str] | None:
        """Commit SHAs for ``(host, repo)`` for mirror comparison, or
        ``None`` when the side is unreadable (unverified, never 'synced')."""
        ...

    def issue_readable(self, ref: int | str) -> bool | None:
        """Does a bound ``issue`` still exist / is it readable? ``True``
        resolves, ``False`` is confirmed gone (404/410), ``None`` could not be
        verified — the crosslink-health tri-state (never 'healthy' on error)."""
        ...


# ---------------------------------------------------------------------------
# GitLab (REST v4, urllib — stdlib only)
# ---------------------------------------------------------------------------


def _get_json(url: str, headers: dict[str, str], *, timeout: int = 15) -> Any:
    """GET + parse JSON, returning ``None`` on any failure. Never raises."""
    import urllib.error
    import urllib.request

    try:
        request = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
            if response.status != 200:
                return None
            return json.load(response)
    except (urllib.error.URLError, OSError, ValueError):
        return None


#: HTTP statuses that confirm a resource is gone — a link is *dead*, not merely
#: unreadable. Anything else that is not a success is treated as unverifiable.
_GONE_STATUSES = (404, 410)


def resolves(url: str, headers: dict[str, str] | None = None, *, timeout: int = 10) -> bool | None:
    """Read-only reachability of a URL, tri-state (the crosslink-health rule).

    ``True`` = a success response, ``False`` = a confirmed-gone status (404/410),
    ``None`` = could not be verified (any other status, a network error, an
    unreachable host). A HEAD is tried first and a GET falls back for a host
    that refuses HEAD; the body is never read. Never raises, never 'healthy' on
    error — an unverifiable link is ``None``, not ``True``."""
    import urllib.error
    import urllib.request

    for method in ("HEAD", "GET"):
        try:
            request = urllib.request.Request(url, headers=headers or {}, method=method)
            with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
                return 200 <= int(response.status) < 400
        except urllib.error.HTTPError as exc:
            if exc.code in _GONE_STATUSES:
                return False
            if method == "HEAD" and exc.code in (405, 501):
                continue  # HEAD not allowed — retry as GET
            return None
        except (urllib.error.URLError, OSError, ValueError):
            return None
    return None


class GitLabClient:
    """GitLab issues + merge requests over the REST API, keyed by project id.

    The token is resolved once from the vault and held in the instance; it is
    sent only as the ``PRIVATE-TOKEN`` header and is never logged.
    """

    def __init__(self, *, host: str, project_id: Any, token: str) -> None:
        self._base = f"https://{host}/api/v4"
        self._project = str(project_id)
        self._headers = {"PRIVATE-TOKEN": token}

    def ping(self) -> bool:
        return _get_json(f"{self._base}/version", self._headers) is not None

    def whoami(self) -> str | None:
        data = _get_json(f"{self._base}/user", self._headers)
        return data.get("username") if isinstance(data, dict) else None

    def project_readable(self) -> bool:
        return _get_json(f"{self._base}/projects/{self._project}", self._headers) is not None

    def issues(self, since: str | None) -> list[TrackerIssue]:
        params = "&updated_after=" + since if since else ""
        raw = _get_json(
            f"{self._base}/projects/{self._project}/issues?per_page=100&scope=all{params}",
            self._headers,
        )
        out: list[TrackerIssue] = []
        for item in raw or []:
            if not isinstance(item, dict):
                continue
            assignee = (item.get("assignee") or {}).get("username")
            milestone = (item.get("milestone") or {}).get("title")
            out.append(
                TrackerIssue(
                    ref=item.get("iid"),
                    title=item.get("title", ""),
                    state=item.get("state", "opened"),
                    assignee=assignee,
                    milestone=milestone,
                    due=item.get("due_date"),
                    labels=tuple(item.get("labels", []) or ()),
                    updated_at=item.get("updated_at"),
                )
            )
        return out

    def merge_requests(self, since: str | None) -> list[MergeRequest]:
        params = "&updated_after=" + since if since else ""
        raw = _get_json(
            f"{self._base}/projects/{self._project}/merge_requests?per_page=100&scope=all{params}",
            self._headers,
        )
        out: list[MergeRequest] = []
        for item in raw or []:
            if not isinstance(item, dict):
                continue
            # GitLab exposes closes-issues on a sub-resource; the lightweight
            # signal here is the MR iid + head sha, with any issue refs the
            # list view already carries.
            out.append(
                MergeRequest(
                    ref=item.get("iid"),
                    title=item.get("title", ""),
                    state=item.get("state", "opened"),
                    author=(item.get("author") or {}).get("username"),
                    issue_refs=tuple(item.get("issue_refs", []) or ()),
                    sha=item.get("sha"),
                    updated_at=item.get("updated_at"),
                )
            )
        return out

    def commits(self, host: str, repo: str) -> set[str] | None:
        encoded = repo.replace("/", "%2F")
        raw = _get_json(
            f"https://{host}/api/v4/projects/{encoded}/repository/commits?per_page=100",
            self._headers,
        )
        if raw is None:
            return None
        return {c.get("id") for c in raw if isinstance(c, dict) and c.get("id")}

    def issue_readable(self, ref: int | str) -> bool | None:
        return resolves(f"{self._base}/projects/{self._project}/issues/{ref}", self._headers)


# ---------------------------------------------------------------------------
# GitHub (via the authenticated gh CLI, mirroring the release extension)
# ---------------------------------------------------------------------------


def _gh_json(args: list[str]) -> Any:
    """Run ``gh`` and parse JSON stdout, or ``None`` on any failure."""
    import subprocess

    try:
        result = subprocess.run(
            ["gh", *args], capture_output=True, text=True, timeout=15, check=False
        )
        if result.returncode != 0 or not result.stdout.strip():
            return None
        return json.loads(result.stdout)
    except (OSError, ValueError, json.JSONDecodeError):
        return None


def _gh_exists(args: list[str]) -> bool | None:
    """Tri-state existence of a ``gh api`` resource: ``True`` (ok), ``False``
    (a confirmed 404/Not Found), ``None`` (any other failure — unverifiable)."""
    import subprocess

    try:
        result = subprocess.run(
            ["gh", *args], capture_output=True, text=True, timeout=15, check=False
        )
        if result.returncode == 0:
            return True
        blurb = (result.stderr or "").lower()
        if "404" in blurb or "not found" in blurb:
            return False
        return None
    except OSError:
        return None


class GitHubCliClient:
    """GitHub issues + PRs through the authenticated ``gh`` CLI.

    ``gh`` carries its own auth, so no token is held here; where a token is
    required (self-hosted, or the mirror-side read) a vault token can be
    passed and surfaced to ``gh`` via its own environment by the caller. This
    client stays read-only.
    """

    def __init__(self, *, repo: str) -> None:
        self._repo = repo  # "owner/name"

    def ping(self) -> bool:
        return _gh_json(["api", "rate_limit"]) is not None

    def whoami(self) -> str | None:
        data = _gh_json(["api", "user"])
        return data.get("login") if isinstance(data, dict) else None

    def project_readable(self) -> bool:
        return _gh_json(["api", f"repos/{self._repo}"]) is not None

    def issues(self, since: str | None) -> list[TrackerIssue]:
        q = f"repos/{self._repo}/issues?state=all&per_page=100"
        if since:
            q += "&since=" + since
        raw = _gh_json(["api", q])
        out: list[TrackerIssue] = []
        for item in raw or []:
            if not isinstance(item, dict) or "pull_request" in item:
                continue  # the issues endpoint also returns PRs; skip those
            assignee = (item.get("assignee") or {}).get("login")
            milestone = (item.get("milestone") or {}).get("title")
            due = (item.get("milestone") or {}).get("due_on")
            out.append(
                TrackerIssue(
                    ref=item.get("number"),
                    title=item.get("title", ""),
                    state=item.get("state", "open"),
                    assignee=assignee,
                    milestone=milestone,
                    due=due[:10] if isinstance(due, str) else None,
                    labels=tuple(label.get("name") for label in item.get("labels", []) if isinstance(label, dict)),
                    updated_at=item.get("updated_at"),
                )
            )
        return out

    def merge_requests(self, since: str | None) -> list[MergeRequest]:
        raw = _gh_json(["api", f"repos/{self._repo}/pulls?state=all&per_page=100"])
        out: list[MergeRequest] = []
        for item in raw or []:
            if not isinstance(item, dict):
                continue
            out.append(
                MergeRequest(
                    ref=item.get("number"),
                    title=item.get("title", ""),
                    state="merged" if item.get("merged_at") else item.get("state", "open"),
                    author=(item.get("user") or {}).get("login"),
                    issue_refs=(),
                    sha=(item.get("head") or {}).get("sha"),
                    updated_at=item.get("updated_at"),
                )
            )
        return out

    def commits(self, host: str, repo: str) -> set[str] | None:
        raw = _gh_json(["api", f"repos/{repo}/commits?per_page=100"])
        if raw is None:
            return None
        return {c.get("sha") for c in raw if isinstance(c, dict) and c.get("sha")}

    def issue_readable(self, ref: int | str) -> bool | None:
        return _gh_exists(["api", f"repos/{self._repo}/issues/{ref}"])


# ---------------------------------------------------------------------------
# The default crosslink checker — a tracker client for issues, a read-only
# HTTP GET/HEAD for declared endpoints
# ---------------------------------------------------------------------------


class ClientLinkChecker:
    """The production :class:`~._capture.LinkChecker`: issue readability rides
    the (already verified, read-only) tracker client; a declared endpoint URL
    is resolved with a credential-less HEAD/GET (endpoints are public pages).

    Both are tri-state and exception-safe — a host that cannot be reached
    yields ``None`` (unverified), never ``True``."""

    def __init__(self, client: TrackerClient | None) -> None:
        self._client = client

    def issue_readable(self, ref: int | str) -> bool | None:
        client = self._client
        if client is None:
            return None
        try:
            return client.issue_readable(ref)
        except Exception:  # noqa: BLE001 — an error is unverified, never 'healthy'
            return None

    def url_resolves(self, url: str) -> bool | None:
        return resolves(url)


__all__ = [
    "CredentialUnavailable",
    "resolve_vault_token",
    "resolves",
    "TrackerClient",
    "GitLabClient",
    "GitHubCliClient",
    "ClientLinkChecker",
]
