# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Webhook receiver — push-based Git event ingest, provider-agnostic.

The companion to ``cross_repo_pr_watch``: that one polls; this one
reacts. Together they cover the spectrum from "low-traffic forge with
no app installed" (poll) to "GitHub App with subscribed events" (push).

Per Ben's 2026-06-01 directive any repo-provider abstraction must use
the Factory/Provider/Adapter pattern. Layout:

  - :class:`RepoEvent` — canonical event shape (provider-agnostic).
  - :class:`EventProvider` Protocol — ``verify`` + ``parse``.
  - :class:`GitHubEventProvider` — HMAC-SHA256 / X-Hub-Signature-256.
  - :class:`GitLabEventProvider` — X-Gitlab-Token equality.
  - :func:`detect_event_provider` — Factory by header.
  - :func:`receive` — top-level orchestration: verify + parse + emit.

What this module is NOT: an HTTP server. Wiring to FastAPI / Flask /
the AEOS ``web`` ext is a thin call into ``receive(headers, body,
secrets)``; the verify+parse logic is pure and unit-testable.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from dataclasses import dataclass, field
from typing import Any, Mapping, Protocol


__all__ = [
    "EventProvider",
    "GitHubEventProvider",
    "GitLabEventProvider",
    "RepoEvent",
    "SignatureMismatch",
    "detect_event_provider",
    "receive",
]


# ---------------------------------------------------------------------------
# Canonical event shape
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RepoEvent:
    """One normalized repo event. ``kind`` is dotted ``"<resource>.<action>"``.

    Examples: ``"push"``, ``"pull_request.opened"``,
    ``"pull_request.synchronize"``, ``"workflow_run.failure"``,
    ``"check_run.completed"``.
    """

    provider: str
    kind: str
    repo: str
    ref: str = ""
    actor: str = ""
    url: str = ""
    verified: bool = False
    """True iff the signature verified against a configured secret."""
    raw: dict[str, Any] = field(default_factory=dict)


class SignatureMismatch(Exception):
    """Raised by :func:`receive` when signature verification fails."""


def _header(headers: Mapping[str, str], name: str) -> str:
    """Case-insensitive header lookup."""
    lname = name.lower()
    for k, v in headers.items():
        if k.lower() == lname:
            return v
    return ""


def _normalize(headers: Mapping[str, str]) -> dict[str, str]:
    """Lowercase-keyed copy for adapter convenience."""
    return {k.lower(): v for k, v in headers.items()}


# ---------------------------------------------------------------------------
# Protocol
# ---------------------------------------------------------------------------


class EventProvider(Protocol):
    name: str

    def verify(
        self, headers: Mapping[str, str], body: bytes, secret: str
    ) -> bool: ...

    def parse(
        self, headers: Mapping[str, str], body: bytes
    ) -> RepoEvent | None: ...


# ---------------------------------------------------------------------------
# GitHub adapter
# ---------------------------------------------------------------------------


@dataclass
class GitHubEventProvider:
    """GitHub App / repo webhook adapter."""

    name: str = "github"

    def verify(
        self, headers: Mapping[str, str], body: bytes, secret: str
    ) -> bool:
        sig = _header(headers, "X-Hub-Signature-256")
        if not sig or not sig.startswith("sha256="):
            return False
        mac = hmac.new(secret.encode(), body, hashlib.sha256)
        expected = "sha256=" + mac.hexdigest()
        # Constant-time compare to avoid timing oracles.
        return hmac.compare_digest(sig, expected)

    def parse(
        self, headers: Mapping[str, str], body: bytes
    ) -> RepoEvent | None:
        event = _header(headers, "X-GitHub-Event")
        if not event:
            return None
        try:
            payload = json.loads(body)
        except (ValueError, TypeError):
            return None
        repo = (payload.get("repository") or {}).get("full_name", "")

        if event == "push":
            return RepoEvent(
                provider=self.name,
                kind="push",
                repo=repo,
                ref=payload.get("ref", ""),
                actor=(payload.get("pusher") or {}).get("name", ""),
                url=payload.get("compare", ""),
                raw=payload,
            )
        if event == "pull_request":
            action = payload.get("action", "")
            pr = payload.get("pull_request") or {}
            return RepoEvent(
                provider=self.name,
                kind=f"pull_request.{action}",
                repo=repo,
                ref=(pr.get("head") or {}).get("ref", ""),
                actor=(pr.get("user") or {}).get("login", ""),
                url=pr.get("html_url", ""),
                raw=payload,
            )
        if event == "workflow_run":
            run = payload.get("workflow_run") or {}
            conclusion = run.get("conclusion") or payload.get("action", "")
            return RepoEvent(
                provider=self.name,
                kind=f"workflow_run.{conclusion}",
                repo=repo,
                ref=run.get("head_branch", ""),
                url=run.get("html_url", ""),
                raw=payload,
            )
        if event == "check_run":
            run = payload.get("check_run") or {}
            return RepoEvent(
                provider=self.name,
                kind=f"check_run.{run.get('conclusion') or payload.get('action', '')}",
                repo=repo,
                ref=run.get("head_sha", ""),
                url=run.get("html_url", ""),
                raw=payload,
            )
        # Unknown event — skip rather than emit a half-shaped record.
        return None


# ---------------------------------------------------------------------------
# GitLab adapter
# ---------------------------------------------------------------------------


@dataclass
class GitLabEventProvider:
    """GitLab project webhook adapter. Signature scheme is plain-token
    equality on ``X-Gitlab-Token`` (GitLab's design; not HMAC)."""

    name: str = "gitlab"

    def verify(
        self, headers: Mapping[str, str], body: bytes, secret: str
    ) -> bool:
        return hmac.compare_digest(
            _header(headers, "X-Gitlab-Token"), secret
        )

    def parse(
        self, headers: Mapping[str, str], body: bytes
    ) -> RepoEvent | None:
        if not _header(headers, "X-Gitlab-Event"):
            return None
        try:
            payload = json.loads(body)
        except (ValueError, TypeError):
            return None
        kind_raw = payload.get("object_kind", "")
        repo = (payload.get("project") or {}).get(
            "path_with_namespace", ""
        )
        if kind_raw == "push":
            return RepoEvent(
                provider=self.name,
                kind="push",
                repo=repo,
                ref=payload.get("ref", ""),
                actor=payload.get("user_username", ""),
                raw=payload,
            )
        if kind_raw == "merge_request":
            attrs = payload.get("object_attributes") or {}
            return RepoEvent(
                provider=self.name,
                kind=f"pull_request.{attrs.get('action', '')}",
                repo=repo,
                ref=attrs.get("source_branch", ""),
                actor=payload.get("user", {}).get("username", ""),
                url=attrs.get("url", ""),
                raw=payload,
            )
        return None


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def detect_event_provider(
    headers: Mapping[str, str],
) -> EventProvider | None:
    """Resolve the right adapter from request headers.

    Returns ``None`` for unknown providers so callers can return 404
    rather than mis-route an event.
    """
    h = _normalize(headers)
    if "x-github-event" in h:
        return GitHubEventProvider()
    if "x-gitlab-event" in h:
        return GitLabEventProvider()
    return None


# ---------------------------------------------------------------------------
# Top-level entry point
# ---------------------------------------------------------------------------


def receive(
    headers: Mapping[str, str],
    body: bytes,
    *,
    secrets: Mapping[str, str],
    require_verified: bool = True,
) -> RepoEvent | None:
    """Verify + parse + return a canonical event.

    Parameters
    ----------
    headers, body:
        The raw HTTP inputs from the calling adapter (FastAPI / Flask /
        the AEOS web ext).
    secrets:
        Per-provider shared secret. Keyed by provider name
        (``"github"`` / ``"gitlab"``). Missing keys behave per
        ``require_verified``.
    require_verified:
        When ``True`` (default; production), missing-secret or
        signature-mismatch raises :class:`SignatureMismatch`. When
        ``False`` (dev/local), the event is returned with
        ``verified=False`` so callers can decide.

    Returns
    -------
    RepoEvent | None
        ``None`` only when the provider is unknown. Otherwise either a
        verified event, or — in dev mode — an unverified one.
    """
    provider = detect_event_provider(headers)
    if provider is None:
        return None

    secret = secrets.get(provider.name, "")
    verified = False
    if secret:
        verified = provider.verify(headers, body, secret)
        if not verified and require_verified:
            raise SignatureMismatch(
                f"{provider.name} webhook signature did not verify"
            )
    elif require_verified:
        raise SignatureMismatch(
            f"no secret configured for {provider.name}; refusing event"
        )

    evt = provider.parse(headers, body)
    if evt is None:
        return None
    # RepoEvent is frozen — return a copy with the verified flag set.
    return RepoEvent(
        provider=evt.provider,
        kind=evt.kind,
        repo=evt.repo,
        ref=evt.ref,
        actor=evt.actor,
        url=evt.url,
        verified=verified,
        raw=evt.raw,
    )
