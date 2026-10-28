# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Webhook handoff when a built-in repair cannot draft a diff.

One JSON request goes out per pull request, head SHA, and blocker.
The fix writer pushes a normal commit on ``rivet-fix/<pr>-<shortsha>``
cut from that head. The next pass reads the branch, and the actor files
the diff through the same approval gate as a built-in fix. This module
does not push and does not merge.
"""

from __future__ import annotations

import hashlib
import json
import logging
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Callable, Protocol

from axiom.extensions.builtins.release.fixes import Diagnosis, Fix, MechanicalFixProvider
from axiom.extensions.builtins.release.lander.model import OpenPull
from axiom.infra.switchplan.ledger import FileLedger, SwitchRecord, now as ledger_now

_log = logging.getLogger(__name__)

SHORT_SHA = 7

#: Request body. ``diagnosis`` and ``blocker`` are the same kind.
FIX_REQUEST_SCHEMA: dict = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "title": "FixRequest",
    "type": "object",
    "additionalProperties": False,
    "required": [
        "repo",
        "pr",
        "head_sha",
        "base",
        "diagnosis",
        "blocker",
        "failing_checks",
        "branch",
    ],
    "properties": {
        "repo": {"type": "string", "minLength": 1},
        "pr": {"type": "integer", "minimum": 1},
        "head_sha": {"type": "string", "minLength": 1},
        "base": {"type": "string", "minLength": 1},
        "diagnosis": {"type": "string", "minLength": 1},
        "blocker": {"type": "string", "minLength": 1},
        "failing_checks": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["name", "log_url"],
                "properties": {
                    "name": {"type": "string"},
                    "log_url": {"type": "string"},
                },
            },
        },
        "branch": {"type": "string", "pattern": r"^rivet-fix/[0-9]+-[0-9a-f]+$"},
    },
}


class HandoffError(RuntimeError):
    """The webhook could not be sent. The message must not contain the key."""


@dataclass(frozen=True)
class SideBranch:
    """A fix branch cut from a pull-request head."""

    name: str
    sha: str
    base_sha: str
    author: str
    diff: str
    paths: tuple[str, ...] = ()


class BranchSource(Protocol):
    def get(self, repo: str, branch: str, head_sha: str) -> SideBranch | None:
        """The branch, or None when it does not exist."""


class BranchLookupError(RuntimeError):
    """The forge could not be asked. Absence is not this error."""


def fix_branch_name(number: int, head_sha: str) -> str:
    """``rivet-fix/<pr>-<shortsha>``. ``shortsha`` is the first 7 hex chars."""
    short = (head_sha or "").lower()[:SHORT_SHA]
    return f"rivet-fix/{number}-{short}"


def request_id(repo: str, number: int, head_sha: str, blocker: str) -> str:
    raw = f"{repo}\n{number}\n{head_sha}\n{blocker}".encode()
    return hashlib.sha256(raw).hexdigest()


def failing_check_payload(pull: OpenPull, diagnosis: Diagnosis) -> list[dict[str, str]]:
    checks = pull.failing_checks
    if checks:
        return [{"name": item.name, "log_url": item.log_url} for item in checks]
    return [{"name": diagnosis.kind, "log_url": ""}]


def build_fix_request(
    *,
    repo: str,
    pull: OpenPull,
    diagnosis: Diagnosis,
) -> dict:
    """The JSON body. No secrets."""
    body = {
        "repo": repo,
        "pr": pull.number,
        "head_sha": pull.head_sha,
        "base": pull.base_ref or "main",
        "diagnosis": diagnosis.kind,
        "blocker": diagnosis.kind,
        "failing_checks": failing_check_payload(pull, diagnosis),
        "branch": fix_branch_name(pull.number, pull.head_sha),
    }
    _require_request(body)
    return body


def _require_request(body: dict) -> None:
    required = FIX_REQUEST_SCHEMA["required"]
    if list(body) != required and set(body) != set(required):
        extra = set(body) - set(required)
        missing = set(required) - set(body)
        raise HandoffError(f"fix request fields mismatch extra={sorted(extra)} missing={sorted(missing)}")
    if not isinstance(body["pr"], int) or body["pr"] < 1:
        raise HandoffError("fix request pr must be a positive integer")
    for key in ("repo", "head_sha", "base", "diagnosis", "blocker", "branch"):
        if not isinstance(body[key], str) or not body[key]:
            raise HandoffError(f"fix request {key} must be a non-empty string")
    if body["diagnosis"] != body["blocker"]:
        raise HandoffError("diagnosis and blocker are the same kind")
    checks = body["failing_checks"]
    if not isinstance(checks, list):
        raise HandoffError("failing_checks must be a list")
    for item in checks:
        if not isinstance(item, dict) or set(item) != {"name", "log_url"}:
            raise HandoffError("each failing check has name and log_url")
        if not isinstance(item["name"], str) or not isinstance(item["log_url"], str):
            raise HandoffError("failing check name and log_url are strings")


def _redact(message: str, key: str) -> str:
    if key and key in message:
        return message.replace(key, "[redacted]")
    return message


def post_fix_request(
    url: str,
    body: dict,
    *,
    header: str,
    key: str,
    timeout: float = 30,
) -> None:
    """POST ``body`` as JSON. The key is a header value and is never logged."""
    if not url or not key or not header:
        raise HandoffError("webhook url and key are required")
    payload = json.dumps(body).encode("utf-8")
    request = urllib.request.Request(url, data=payload, method="POST")
    request.add_header("Content-Type", "application/json")
    request.add_header(header, key)
    _log.info(
        "handoff request repo=%s pr=%s branch=%s blocker=%s",
        body.get("repo"),
        body.get("pr"),
        body.get("branch"),
        body.get("blocker"),
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            status = getattr(response, "status", 200)
            if status >= 300:
                raise HandoffError(f"webhook status {status}")
    except HandoffError:
        raise
    except Exception as exc:
        raise HandoffError(_redact(str(exc), key)) from None


Poster = Callable[..., None]


def _builtin_can_fix(fix: Fix) -> bool:
    if fix.needs_human or fix.pending or fix.force:
        return False
    if fix.kind in {"land", "rerun"}:
        return True
    return bool(fix.diff or fix.commands)


class GhBranchSource:
    """Read a side branch with ``gh``. A missing branch is None."""

    def __init__(self, runner: Callable[[list[str]], str] | None = None) -> None:
        self._run = runner or _gh

    def get(self, repo: str, branch: str, head_sha: str) -> SideBranch | None:
        encoded = urllib.parse.quote(branch, safe="")
        try:
            raw = self._run(["gh", "api", f"repos/{repo}/compare/{head_sha}...{encoded}"])
        except BranchLookupError:
            raise
        except Exception as exc:
            text = str(exc)
            if "404" in text or "Not Found" in text:
                return None
            raise BranchLookupError(text) from None
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise BranchLookupError("compare response was not JSON") from exc
        status = str(data.get("status") or "")
        behind = int(data.get("behind_by") or 0)
        files = data.get("files") or []
        paths: list[str] = []
        patches: list[str] = []
        for item in files:
            if not isinstance(item, dict):
                continue
            filename = str(item.get("filename") or "")
            if filename:
                paths.append(filename)
            patch = item.get("patch")
            if isinstance(patch, str) and patch:
                patches.append(patch)
        based = status in {"ahead", "identical"} and behind == 0
        author = ""
        commits = data.get("commits") or []
        if commits and isinstance(commits[-1], dict):
            commit = commits[-1].get("commit") or {}
            author_block = commit.get("author") or {}
            author = str(author_block.get("name") or "")
            if not author:
                author = str((commits[-1].get("author") or {}).get("login") or "")
        return SideBranch(
            name=branch,
            sha=str(data.get("commits", [{}])[-1].get("sha") if commits else head_sha),
            base_sha=head_sha if based else "",
            author=author,
            diff="" if status == "identical" else "\n".join(patches),
            paths=tuple(paths),
        )


def _gh(args: list[str]) -> str:
    import subprocess

    done = subprocess.run(args, capture_output=True, text=True, timeout=60, check=False)
    if done.returncode != 0:
        detail = (done.stderr or done.stdout or "gh failed").strip()
        if "404" in detail or "Not Found" in detail:
            raise FileNotFoundError(detail)
        raise BranchLookupError(detail[:300])
    return done.stdout


class WebhookHandoffFixProvider:
    """Built-in repair first. Otherwise one webhook, then the side branch.

    Registered as ``handoff`` on ``axiom.release.fix_provider``.
    """

    name = "handoff"

    def __init__(
        self,
        *,
        inner: MechanicalFixProvider | None = None,
        poster: Poster | None = None,
        branches: BranchSource | None = None,
        config: object | None = None,
        state_dir: object | None = None,
        repo: str = "",
        environ: dict[str, str] | None = None,
    ) -> None:
        self.inner = inner or MechanicalFixProvider()
        self.poster = poster or post_fix_request
        self.branches = branches or GhBranchSource()
        self.config = config
        self.state_dir = state_dir
        self.repo = repo
        self.environ = environ

    def bind(self, *, config: object, state_dir: object, repo: str, environ: dict[str, str]) -> None:
        self.config = config
        self.state_dir = state_dir
        self.repo = repo
        self.environ = environ

    def diagnose(self, pull: OpenPull) -> Diagnosis:
        return self.inner.diagnose(pull)

    def draft(self, diagnosis: Diagnosis, pull: OpenPull) -> Fix:
        if pull.draft:
            return Fix(summary="draft", needs_human=True, reason="draft")
        if diagnosis.kind == "clear":
            return Fix(summary="land", kind="land")
        branch_fix = self._from_branch(pull)
        if branch_fix is not None:
            return branch_fix
        builtin = self.inner.draft(diagnosis, pull)
        if _builtin_can_fix(builtin) or builtin.reason == "human_route":
            return builtin
        if self.config is not None:
            from axiom.extensions.builtins.release.lander.actor import fixer_block

            blocked = fixer_block(pull, self.config)
            if blocked:
                return Fix(summary=blocked, needs_human=True, reason=blocked)
        return self._request(diagnosis, pull)

    def _from_branch(self, pull: OpenPull) -> Fix | None:
        name = fix_branch_name(pull.number, pull.head_sha)
        try:
            found = self.branches.get(self.repo, name, pull.head_sha)
        except BranchLookupError:
            return Fix(summary="branch lookup failed", needs_human=True, reason="branch-lookup-failed")
        if found is None:
            return None
        if found.base_sha != pull.head_sha or not found.diff.strip():
            return None
        return Fix(
            summary=f"side branch {found.name}",
            diff=found.diff if found.diff.endswith("\n") else found.diff + "\n",
            kind="repair",
            writer=found.author,
            branch=found.name,
            target_ref=pull.head_ref,
            paths=found.paths or pull.files,
        )

    def _request(self, diagnosis: Diagnosis, pull: OpenPull) -> Fix:
        config = self.config
        url_env = str(getattr(config, "webhook_url_env", "") or "")
        key_env = str(getattr(config, "webhook_key_env", "") or "")
        header = str(getattr(config, "webhook_key_header", "") or "X-Fix-Key")
        env = self.environ if self.environ is not None else {}
        url = env.get(url_env, "") if url_env else ""
        key = env.get(key_env, "") if key_env else ""
        if not url or not key:
            return Fix(summary=diagnosis.kind, needs_human=True, reason="no fixer configured")
        try:
            body = build_fix_request(repo=self.repo, pull=pull, diagnosis=diagnosis)
        except HandoffError as exc:
            return Fix(summary=diagnosis.kind, needs_human=True, reason=str(exc)[:200])
        record_key = request_id(self.repo, pull.number, pull.head_sha, diagnosis.kind)
        ledger = self._ledger()
        if ledger is not None and ledger.load(record_key) is not None:
            return Fix(
                summary="handoff already requested",
                kind="handoff",
                pending=True,
                branch=body["branch"],
                reason="handoff",
            )
        try:
            self.poster(url, body, header=header, key=key)
        except Exception as exc:
            return Fix(
                summary=diagnosis.kind,
                needs_human=True,
                reason=f"webhook-failed: {_redact(str(exc), key)[:200]}",
            )
        self._record(record_key, body)
        return Fix(
            summary="waiting on the fix branch",
            kind="handoff",
            pending=True,
            branch=body["branch"],
            reason="handoff",
        )

    def _ledger(self) -> FileLedger | None:
        if self.state_dir is None:
            return None
        return FileLedger(_ledger_dir(self.state_dir))

    def _record(self, record_key: str, body: dict) -> None:
        ledger = self._ledger()
        if ledger is None:
            return
        moment = ledger_now().isoformat()
        record = SwitchRecord(
            id=record_key,
            kind="release.handoff",
            subject=f"{body['repo']}#{body['pr']}",
            phase="committed",
            created_at=moment,
            updated_at=moment,
            state={
                "repo": body["repo"],
                "pr": body["pr"],
                "head_sha": body["head_sha"],
                "blocker": body["blocker"],
                "branch": body["branch"],
            },
        )
        record.event("handoff", f"{body['pr']} {body['blocker']} {body['branch']}")
        ledger.save(record)


def _ledger_dir(state_dir: object):
    from pathlib import Path

    return Path(state_dir) / "agents" / "rivet" / "lander-records"


__all__ = [
    "FIX_REQUEST_SCHEMA",
    "BranchLookupError",
    "BranchSource",
    "GhBranchSource",
    "HandoffError",
    "SideBranch",
    "WebhookHandoffFixProvider",
    "build_fix_request",
    "fix_branch_name",
    "post_fix_request",
    "request_id",
]
