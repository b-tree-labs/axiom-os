# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""GitHub over REST with GitHub App auth — no ``gh`` CLI, no personal token.

A program tracked across many repositories needs one credential that reads
all of them and belongs to no person. A GitHub App installation is that
credential: the deployment registers an App, installs it on its account,
and stores the App's private key in the Axiom vault. This module:

1. mints a short-lived App JWT (RS256, ten minutes at most) from the key;
2. exchanges it for an **installation token** that asks for exactly the
   permissions the caller names (read-only by default: ``issues``,
   ``pull_requests``, ``contents``, ``metadata``), caching it until shortly
   before it expires;
3. reads the REST API over HTTPS with that token
   (:class:`GitHubRestClient`, a :class:`~._clients.TrackerClient`).

The private key and every token live only inside the provider instance.
Neither is logged, printed, placed on argv, or carried in an exception
message: an error says which step failed and the HTTP status, nothing else.

The deployment declares the App in ``program.github_app``::

    {"app_id": 12345, "installation_id": 67890, "credential": "<vault name>"}

``org`` may stand in for ``installation_id`` (the installation is looked up
on the account). With no ``github_app`` block the program keeps using the
``gh`` CLI client, so an existing deployment changes nothing.

Every HTTP call goes through an injectable ``transport`` so unit tests never
touch the network.
"""

from __future__ import annotations

import json
import re
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from ..model import ProgramError
from ._capture import MergeRequest, TrackerIssue
from ._clients import TrackerReadError
from ._work import WorkItem, parse_tracker_crosslink

#: The read-only installation permissions a tracker read asks for.
READ_PERMISSIONS: dict[str, str] = {
    "issues": "read",
    "pull_requests": "read",
    "contents": "read",
    "metadata": "read",
}

#: Seconds before expiry at which a cached installation token is replaced.
_REFRESH_MARGIN = 300
#: Hard ceiling on pages per list read (100 pages x 100 items).
_MAX_PAGES = 100


class GitHubAppError(ProgramError):
    """The App could not authenticate. The message never carries a secret."""


# ---------------------------------------------------------------------------
# Transport (injectable — tests pass a fake; production uses urllib)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class HttpResponse:
    status: int
    headers: dict[str, str] = field(default_factory=dict)
    body: bytes = b""

    def json(self) -> Any:
        try:
            return json.loads(self.body.decode("utf-8") or "null")
        except (UnicodeDecodeError, ValueError):
            return None


Transport = Callable[[str, str, dict[str, str], bytes | None], HttpResponse]


def urllib_transport(method: str, url: str, headers: dict[str, str], body: bytes | None) -> HttpResponse:
    """The production transport: one HTTPS request, never raises for a status.

    A transport error is reported as status ``0`` so the caller decides; the
    request headers (which carry the token) are never part of any message.
    """
    import urllib.error
    import urllib.request

    request = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310
            return HttpResponse(
                status=int(response.status),
                headers={k.lower(): v for k, v in response.headers.items()},
                body=response.read(),
            )
    except urllib.error.HTTPError as exc:
        return HttpResponse(
            status=int(exc.code),
            headers={k.lower(): v for k, v in (exc.headers or {}).items()},
            body=b"",
        )
    except (urllib.error.URLError, OSError, ValueError):
        return HttpResponse(status=0)


# ---------------------------------------------------------------------------
# App config + token minting
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class GitHubAppConfig:
    """The deployment's ``program.github_app`` block, shape-checked."""

    app_id: str
    credential: str
    installation_id: str | None = None
    org: str | None = None
    api_base: str = "https://api.github.com"

    @classmethod
    def from_raw(cls, raw: Any) -> GitHubAppConfig | None:
        if not isinstance(raw, dict):
            return None
        app_id = raw.get("app_id")
        credential = raw.get("credential")
        if app_id in (None, "") or not isinstance(credential, str) or not credential:
            return None
        installation = raw.get("installation_id")
        org = raw.get("org")
        if installation in (None, "") and not (isinstance(org, str) and org):
            return None
        api = raw.get("api_base")
        return cls(
            app_id=str(app_id),
            credential=credential,
            installation_id=str(installation) if installation not in (None, "") else None,
            org=org if isinstance(org, str) and org else None,
            api_base=(api.rstrip("/") if isinstance(api, str) and api else "https://api.github.com"),
        )


def mint_app_jwt(app_id: str, private_key_pem: str, *, now: float | None = None) -> str:
    """An App JWT: ``iss`` = app id, issued a minute in the past (clock skew),
    expiring in nine minutes (GitHub's ceiling is ten)."""
    import jwt  # PyJWT, a base dependency

    issued = int(now if now is not None else time.time()) - 60
    try:
        return jwt.encode(
            {"iat": issued, "exp": issued + 600 - 60, "iss": str(app_id)},
            private_key_pem,
            algorithm="RS256",
        )
    except Exception as exc:  # noqa: BLE001 — never echo the key or its parse error text
        raise GitHubAppError(f"could not sign the App JWT ({type(exc).__name__})") from None


def _parse_expiry(value: Any) -> float | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


class GitHubAppTokenProvider:
    """Mint and cache installation tokens for one App installation.

    ``key_loader`` returns the PEM private key; production reads it from the
    vault by the configured credential name at first use. ``permissions``
    and ``repositories`` narrow the token: a tracker read asks for
    :data:`READ_PERMISSIONS`; the wiki publisher asks for ``contents:write``
    on the one wiki repository.
    """

    def __init__(
        self,
        config: GitHubAppConfig,
        *,
        key_loader: Callable[[], str],
        transport: Transport | None = None,
        permissions: dict[str, str] | None = None,
        repositories: list[str] | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.config = config
        self._key_loader = key_loader
        self._transport = transport or urllib_transport
        self.permissions = dict(permissions or READ_PERMISSIONS)
        self.repositories = list(repositories) if repositories else None
        self._clock = clock
        self._token: str | None = None
        self._expires: float = 0.0
        self._installation_id = config.installation_id
        self._lock = threading.Lock()

    def __repr__(self) -> str:  # never show the key or the token
        return f"GitHubAppTokenProvider(app_id={self.config.app_id!r}, token=<redacted>)"

    def _app_headers(self) -> dict[str, str]:
        jwt_token = mint_app_jwt(self.config.app_id, self._key_loader(), now=self._clock())
        return {
            "Authorization": f"Bearer {jwt_token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "axiom-program",
        }

    def installation_id(self) -> str:
        if self._installation_id:
            return self._installation_id
        org = self.config.org
        response = self._transport(
            "GET", f"{self.config.api_base}/orgs/{org}/installation", self._app_headers(), None
        )
        if response.status != 200:
            raise GitHubAppError(f"no App installation found for the account (HTTP {response.status})")
        body = response.json()
        ident = body.get("id") if isinstance(body, dict) else None
        if ident in (None, ""):
            raise GitHubAppError("the installation lookup returned no id")
        self._installation_id = str(ident)
        return self._installation_id

    def token(self) -> str:
        """A valid installation token, minting a new one when the cached one
        is within :data:`_REFRESH_MARGIN` of expiry."""
        with self._lock:
            if self._token and self._clock() < self._expires - _REFRESH_MARGIN:
                return self._token
            install = self.installation_id()
            payload: dict[str, Any] = {"permissions": self.permissions}
            if self.repositories:
                payload["repositories"] = self.repositories
            response = self._transport(
                "POST",
                f"{self.config.api_base}/app/installations/{install}/access_tokens",
                {**self._app_headers(), "Content-Type": "application/json"},
                json.dumps(payload).encode("utf-8"),
            )
            if response.status != 201:
                raise GitHubAppError(f"installation token request refused (HTTP {response.status})")
            body = response.json()
            token = body.get("token") if isinstance(body, dict) else None
            if not isinstance(token, str) or not token:
                raise GitHubAppError("installation token response carried no token")
            self._token = token
            self._expires = _parse_expiry(body.get("expires_at")) or (self._clock() + 3600)
            return token


def vault_key_loader(credential: str, state_dir: Any) -> Callable[[], str]:
    """A key loader reading the App's PEM key from the vault on first use."""
    cache: dict[str, str] = {}

    def load() -> str:
        if "key" not in cache:
            from ._clients import resolve_vault_token

            cache["key"] = resolve_vault_token(credential, state_dir)
        return cache["key"]

    return load


# ---------------------------------------------------------------------------
# The REST client (a TrackerClient)
# ---------------------------------------------------------------------------

_LINK_NEXT = re.compile(r'<([^>]+)>\s*;\s*rel="next"')


def _iso(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _after(stamp: str | None, since: str | None) -> bool:
    if not since:
        return True
    if not stamp:
        return False
    return stamp >= since  # GitHub stamps are uniform ISO-8601 Z strings


class GitHubRestClient:
    """GitHub issues, pull requests and commits over REST, App-authenticated.

    Read-only. ``repo`` is the program's home tracker (``owner/name``); the
    work-repository reads take their repository explicitly, because one App
    installation reads every repository it is installed on.
    """

    def __init__(
        self,
        *,
        repo: str,
        tokens: GitHubAppTokenProvider,
        transport: Transport | None = None,
    ) -> None:
        self._repo = repo
        self._tokens = tokens
        self._transport = transport or tokens._transport
        self._api = tokens.config.api_base

    def __repr__(self) -> str:
        return f"GitHubRestClient(repo={self._repo!r})"

    # -- plumbing -----------------------------------------------------------

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._tokens.token()}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "axiom-program",
        }

    def _get(self, path: str) -> HttpResponse:
        return self._transport("GET", f"{self._api}/{path.lstrip('/')}", self._headers(), None)

    def _get_ok(self, path: str) -> Any:
        try:
            response = self._get(path)
        except GitHubAppError:
            return None
        return response.json() if response.status == 200 else None

    def _get_all(self, path: str, *, key: str | None = None) -> list[Any]:
        """Every page of a list endpoint, or raise :class:`TrackerReadError`
        (never a silent prefix). ``key`` names the list inside an object body
        (``/installation/repositories`` wraps its list)."""
        url: str | None = f"{self._api}/{path.lstrip('/')}"
        out: list[Any] = []
        for _ in range(_MAX_PAGES):
            assert url is not None
            try:
                response = self._transport("GET", url, self._headers(), None)
            except GitHubAppError as exc:
                raise TrackerReadError(str(exc)) from None
            if response.status != 200:
                raise TrackerReadError(f"github read returned HTTP {response.status}")
            body = response.json()
            if key is not None and isinstance(body, dict):
                body = body.get(key)
            if not isinstance(body, list):
                raise TrackerReadError("github read returned a non-list body")
            out.extend(body)
            match = _LINK_NEXT.search(response.headers.get("link", ""))
            if not match:
                return out
            url = match.group(1)
        raise TrackerReadError(f"github read did not finish within {_MAX_PAGES} pages")

    # -- the TrackerClient protocol ------------------------------------------

    def ping(self) -> bool:
        try:
            return self._get("rate_limit").status == 200
        except GitHubAppError:
            return False

    def whoami(self) -> str | None:
        """An installation token has no user; the identity is the App
        installation, reported once a token can actually be minted."""
        try:
            self._tokens.token()
        except Exception:  # noqa: BLE001 — failure to mint is "not authenticated"
            return None
        return f"app:{self._tokens.config.app_id}"

    def project_readable(self) -> bool:
        return self._get_ok(f"repos/{self._repo}") is not None

    def issues(self, since: str | None) -> list[TrackerIssue]:
        q = f"repos/{self._repo}/issues?state=all&per_page=100"
        if since:
            q += "&since=" + since
        out: list[TrackerIssue] = []
        for item in self._get_all(q):
            if not isinstance(item, dict) or "pull_request" in item:
                continue
            milestone = item.get("milestone") or {}
            due = milestone.get("due_on") if isinstance(milestone, dict) else None
            out.append(
                TrackerIssue(
                    ref=item.get("number"),
                    title=item.get("title", "") or "",
                    state=item.get("state", "open"),
                    assignee=(item.get("assignee") or {}).get("login"),
                    milestone=milestone.get("title") if isinstance(milestone, dict) else None,
                    due=due[:10] if isinstance(due, str) else None,
                    labels=tuple(
                        label.get("name") for label in item.get("labels", []) if isinstance(label, dict)
                    ),
                    updated_at=item.get("updated_at"),
                    crosslink=parse_tracker_crosslink(item.get("body")),
                )
            )
        return out

    def merge_requests(self, since: str | None) -> list[MergeRequest]:
        q = f"repos/{self._repo}/pulls?state=all&sort=updated&direction=desc&per_page=100"
        out: list[MergeRequest] = []
        for item in self._get_all(q):
            if not isinstance(item, dict) or not _after(item.get("updated_at"), since):
                continue
            out.append(
                MergeRequest(
                    ref=item.get("number"),
                    title=item.get("title", "") or "",
                    state="merged" if item.get("merged_at") else item.get("state", "open"),
                    author=(item.get("user") or {}).get("login"),
                    issue_refs=(),
                    sha=(item.get("head") or {}).get("sha"),
                    updated_at=item.get("updated_at"),
                )
            )
        return out

    def commits(self, host: str, repo: str) -> set[str] | None:
        raw = self._get_ok(f"repos/{repo}/commits?per_page=100")
        if not isinstance(raw, list):
            return None
        return {c.get("sha") for c in raw if isinstance(c, dict) and c.get("sha")}

    def issue_readable(self, ref: int | str) -> bool | None:
        try:
            status = self._get(f"repos/{self._repo}/issues/{ref}").status
        except GitHubAppError:
            return None
        if status == 200:
            return True
        if status in (404, 410):
            return False
        return None

    # -- the work-repository reads ----------------------------------------

    def work_items(self, repo: str, since: str | None) -> list[WorkItem]:
        """Issues and pull requests in ``repo`` updated since the cursor.

        The issues endpoint returns both; a pull request carries
        ``pull_request.merged_at``. The title and body are read only to find
        program links (:mod:`._work`) and are never stored.
        """
        q = f"repos/{repo}/issues?state=all&per_page=100"
        if since:
            q += "&since=" + since
        out: list[WorkItem] = []
        for item in self._get_all(q):
            if not isinstance(item, dict):
                continue
            out.append(work_item_from_rest(repo, item))
        return out

    def org_repos(self, org: str) -> list[dict[str, Any]]:
        """The repositories this installation reads that belong to ``org``."""
        repos = self._get_all("installation/repositories?per_page=100", key="repositories")
        out: list[dict[str, Any]] = []
        for repo in repos:
            if not isinstance(repo, dict):
                continue
            owner = (repo.get("owner") or {}).get("login")
            if isinstance(owner, str) and owner.lower() == org.lower():
                out.append(
                    {
                        "full_name": repo.get("full_name"),
                        "archived": bool(repo.get("archived")),
                        "topics": list(repo.get("topics") or []),
                    }
                )
        return out


def work_item_from_rest(repo: str, item: dict[str, Any]) -> WorkItem:
    """Normalize one issues-endpoint row (issue or pull request)."""
    pr = item.get("pull_request")
    is_pr = isinstance(pr, dict)
    merged_at = _iso(pr.get("merged_at")) if is_pr else None
    if is_pr:
        state = "merged" if merged_at else item.get("state", "open")
    else:
        state = item.get("state", "open")
    text = "\n".join(str(x) for x in (item.get("title"), item.get("body")) if x)
    return WorkItem(
        repo=repo,
        kind="pr" if is_pr else "issue",
        number=item.get("number"),
        state=state,
        url=item.get("html_url") or "",
        text=text,
        updated_at=_iso(item.get("updated_at")),
        done_at=merged_at or _iso(item.get("closed_at")),
    )


# ---------------------------------------------------------------------------
# Choosing the client: App when declared, the gh CLI otherwise
# ---------------------------------------------------------------------------


def build_github_client(
    program: dict[str, Any],
    state_dir: Any,
    *,
    repo: str,
    transport: Transport | None = None,
) -> Any:
    """The program's GitHub client: App-authenticated REST when
    ``program.github_app`` is declared, else the ``gh`` CLI fallback."""
    config = GitHubAppConfig.from_raw(program.get("github_app"))
    if config is None:
        from ._clients import GitHubCliClient

        return GitHubCliClient(repo=repo)
    tokens = GitHubAppTokenProvider(
        config,
        key_loader=vault_key_loader(config.credential, state_dir),
        transport=transport,
    )
    return GitHubRestClient(repo=repo, tokens=tokens, transport=transport)


__all__ = [
    "READ_PERMISSIONS",
    "GitHubAppError",
    "HttpResponse",
    "GitHubAppConfig",
    "GitHubAppTokenProvider",
    "GitHubRestClient",
    "mint_app_jwt",
    "urllib_transport",
    "vault_key_loader",
    "work_item_from_rest",
    "build_github_client",
]
