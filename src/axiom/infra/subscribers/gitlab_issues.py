# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""GitLab issue provider + EventBus subscriber for self-healing signals.

Self-registers with IssueProviderFactory on import. No-ops gracefully
if python-gitlab is not installed or GITLAB_TOKEN is not set.

Config env vars:
    GITLAB_TOKEN         — Personal access token (required)
    GITLAB_URL           — Instance URL (default: gitlab.com)
    NEUT_GITLAB_PROJECT  — Project path (default: empty, must be configured)
"""

from __future__ import annotations

import os
from typing import Any

from axiom.infra.subscribers.issue_provider import IssueProvider, IssueProviderFactory

_DEFAULT_URL = "https://gitlab.com"
_DEFAULT_PROJECT = ""  # Must be configured via env or config


class GitLabIssueProvider(IssueProvider):
    """Files issues on a GitLab project."""

    def __init__(self, config: dict[str, Any] | None = None):
        config = config or {}
        self._url = config.get("url") or os.environ.get("GITLAB_URL", _DEFAULT_URL)
        self._token = config.get("token") or os.environ.get("GITLAB_TOKEN", "")
        self._project_path = (
            config.get("project")
            or os.environ.get("NEUT_GITLAB_PROJECT", _DEFAULT_PROJECT)
        )
        self._gl = None
        self._project = None

    def _connect(self) -> bool:
        """Lazy-connect to GitLab. Returns True if connected."""
        if self._project is not None:
            return True
        if not self._token:
            return False
        try:
            import gitlab
            self._gl = gitlab.Gitlab(self._url, private_token=self._token)
            self._gl.auth()
            self._project = self._gl.projects.get(self._project_path)
            return True
        except Exception:
            return False

    def _get_project(self):
        """Return the project object, raising if not connected."""
        assert self._project is not None, "must call _connect() first"
        return self._project

    def available(self) -> bool:
        return self._connect()

    def find_existing(self, fingerprint: str) -> str | None:
        """Search open issues for a matching [self-heal] fingerprint."""
        if not self._connect():
            return None
        try:
            issues = self._get_project().issues.list(
                state="opened",
                labels=["self-heal"],
                search=fingerprint,
                per_page=5,
            )
            for issue in issues:
                if fingerprint in (issue.title or "") or fingerprint in (issue.description or ""):
                    return issue.web_url
        except Exception:
            pass
        return None

    def add_comment(self, issue_url: str, body: str) -> bool:
        """Add a comment to an existing GitLab issue."""
        if not self._connect():
            return False
        try:
            # Extract issue IID from URL
            # URL format: https://gitlab.example.com/group/project/-/issues/123
            iid = int(issue_url.rstrip("/").split("/")[-1])
            issue = self._get_project().issues.get(iid)
            issue.notes.create({"body": body})
            return True
        except Exception:
            return False

    def update_issue_labels(self, issue_url: str, add_labels: list[str] | None = None,
                           remove_labels: list[str] | None = None) -> bool:
        """Add or remove labels on a GitLab issue."""
        if not self._connect():
            return False
        try:
            iid = int(issue_url.rstrip("/").split("/")[-1])
            issue = self._get_project().issues.get(iid)
            current = set(issue.labels)
            if add_labels:
                current.update(add_labels)
            if remove_labels:
                current -= set(remove_labels)
            issue.labels = list(current)
            issue.save()
            return True
        except Exception:
            return False

    def close_issue(self, issue_url: str) -> bool:
        """Close a GitLab issue."""
        if not self._connect():
            return False
        try:
            iid = int(issue_url.rstrip("/").split("/")[-1])
            issue = self._get_project().issues.get(iid)
            issue.state_event = "close"
            issue.save()
            return True
        except Exception:
            return False

    def create_issue(self, title: str, body: str, labels: list[str]) -> str:
        """Create a GitLab issue with the given title, body, and labels."""
        if not self._connect():
            return ""
        try:
            # Ensure labels exist (GitLab creates them on use)
            issue = self._get_project().issues.create({
                "title": title,
                "description": body,
                "labels": ",".join(labels),
            })
            return issue.web_url
        except Exception:
            return ""


# Self-register
IssueProviderFactory.register("gitlab", GitLabIssueProvider)


# ---------------------------------------------------------------------------
# EventBus subscriber
# ---------------------------------------------------------------------------

def _format_issue_body(data: dict[str, Any]) -> str:
    """Format an error event into a GitLab issue description.

    Handles both direct cli.arg_error events and doctor failure events
    (doctor.patch_failed, doctor.llm_unavailable) which nest the original
    error signal under data["error_signal"].
    """
    # Doctor failure events nest the original error under "error_signal"
    error_signal = data.get("error_signal", data)

    lines = [
        "## CLI Error (Auto-Filed)",
        "",
        f"**Command:** `{' '.join(error_signal.get('argv', []))}`",
        f"**Error type:** `{error_signal.get('error_type', 'unknown')}`",
        f"**Error message:** {error_signal.get('error_message', '')}",
        f"**Recovered:** {'Yes' if error_signal.get('recovered') else 'No'}",
        f"**Fingerprint:** `{data.get('fingerprint', '')}`",
        f"**Timestamp:** {error_signal.get('timestamp', '')}",
    ]

    # Doctor diagnosis (if available from a failed patch attempt)
    diagnosis = data.get("diagnosis", "")
    if diagnosis:
        lines.extend(["", "### Doctor Diagnosis", "", diagnosis])

    patch_diff = data.get("patch_diff", "")
    if patch_diff:
        lines.extend(["", "### Attempted Patch", "", "```diff", patch_diff, "```"])

    tests_output = data.get("tests_output", "")
    if tests_output:
        lines.extend(["", "### Test Output", "", "```", tests_output[:2000], "```"])

    # Traceback
    tb = error_signal.get("traceback", "")
    if tb:
        lines.extend(["", "### Traceback", "", "```", tb.rstrip(), "```"])

    # Environment
    env = error_signal.get("environment", {})
    if env:
        lines.extend(["", "### Environment", ""])
        for key, val in env.items():
            lines.append(f"- **{key}:** `{val}`")

    lines.extend(["", "---", "*Filed automatically by the Neut self-healing pipeline.*"])
    return "\n".join(lines)


def gitlab_issue_handler(topic: str, data: dict[str, Any]) -> None:
    """EventBus subscriber — files GitLab issues for unrecovered CLI errors.

    Handles three topic families:
    - cli.arg_error: Direct CLI errors (legacy path, if doctor not installed)
    - doctor.patch_failed: Doctor tried and failed to fix the error
    - doctor.llm_unavailable: Doctor couldn't run (no LLM)

    Deduplicates by fingerprint. Skips recovered errors.
    """
    # For direct CLI errors, skip recovered ones
    if topic.startswith("cli.") and data.get("recovered"):
        return

    # For doctor failure topics, the original error is nested
    error_signal = data.get("error_signal", data)
    if error_signal.get("recovered"):
        return

    try:
        provider = IssueProviderFactory.create("gitlab")
    except ValueError:
        return

    if not provider.available():
        return

    fingerprint = data.get("fingerprint", "")

    # Dedup — skip if an open issue already exists
    if provider.find_existing(fingerprint):
        return

    # Build title based on topic
    argv_str = " ".join(error_signal.get("argv", []))
    if topic == "doctor.patch_failed":
        title = f"[self-heal] Doctor failed: {argv_str}"
    elif topic == "doctor.llm_unavailable":
        title = f"[self-heal] Undiagnosed error (no LLM): {argv_str}"
    else:
        title = f"[self-heal] CLI error: {argv_str}"

    # Truncate title to 255 chars (GitLab limit)
    if len(title) > 255:
        title = title[:252] + "..."

    labels = ["self-heal"]
    if topic.startswith("doctor."):
        labels.append("doctor-agent")

    body = _format_issue_body(data)
    provider.create_issue(title, body, labels=labels)
