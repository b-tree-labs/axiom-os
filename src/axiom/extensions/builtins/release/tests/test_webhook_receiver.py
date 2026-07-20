# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tests for ``webhook_receiver`` — push-based Git event ingest.

Cross-provider via Factory/Protocol/Adapter (Ben directive 2026-06-01).
"""

from __future__ import annotations

import hashlib
import hmac
import json

import pytest

from axiom.extensions.builtins.release.webhook_receiver import (
    GitHubEventProvider,
    GitLabEventProvider,
    RepoEvent,
    SignatureMismatch,
    detect_event_provider,
    receive,
)


# ---------------------------------------------------------------------------
# Factory — which provider for which headers?
# ---------------------------------------------------------------------------


class TestProviderFactory:
    def test_github_header_routes_to_github(self):
        p = detect_event_provider({"X-GitHub-Event": "push"})
        assert isinstance(p, GitHubEventProvider)

    def test_gitlab_header_routes_to_gitlab(self):
        p = detect_event_provider({"X-Gitlab-Event": "Push Hook"})
        assert isinstance(p, GitLabEventProvider)

    def test_headers_case_insensitive(self):
        p = detect_event_provider({"x-github-event": "push"})
        assert isinstance(p, GitHubEventProvider)

    def test_unknown_headers_returns_none(self):
        assert detect_event_provider({"X-Random": "x"}) is None


# ---------------------------------------------------------------------------
# GitHub adapter — HMAC-SHA256 signature + payload parsing
# ---------------------------------------------------------------------------


def _gh_sign(secret: str, body: bytes) -> str:
    mac = hmac.new(secret.encode(), body, hashlib.sha256)
    return "sha256=" + mac.hexdigest()


class TestGitHubProvider:
    def test_verify_signature_round_trip(self):
        secret = "topsecret"
        body = b'{"action":"opened"}'
        sig = _gh_sign(secret, body)
        prov = GitHubEventProvider()
        assert prov.verify({"X-Hub-Signature-256": sig}, body, secret) is True

    def test_verify_wrong_signature_rejected(self):
        prov = GitHubEventProvider()
        assert (
            prov.verify(
                {"X-Hub-Signature-256": "sha256=" + "0" * 64},
                b'{"action":"opened"}',
                "secret",
            )
            is False
        )

    def test_verify_missing_signature_rejected(self):
        prov = GitHubEventProvider()
        assert prov.verify({}, b"{}", "secret") is False

    def test_parse_pull_request_opened(self):
        body = json.dumps(
            {
                "action": "opened",
                "pull_request": {
                    "number": 42,
                    "head": {"ref": "feat/x"},
                    "html_url": "https://github.com/acme/repo/pull/42",
                    "user": {"login": "@me"},
                },
                "repository": {"full_name": "acme/repo"},
            }
        ).encode()
        prov = GitHubEventProvider()
        evt = prov.parse({"X-GitHub-Event": "pull_request"}, body)
        assert evt is not None
        assert evt.kind == "pull_request.opened"
        assert evt.repo == "acme/repo"
        assert evt.ref == "feat/x"
        assert evt.actor == "@me"

    def test_parse_push(self):
        body = json.dumps(
            {
                "ref": "refs/heads/main",
                "repository": {"full_name": "acme/repo"},
                "pusher": {"name": "@me"},
                "compare": "https://github.com/acme/repo/compare/x...y",
            }
        ).encode()
        prov = GitHubEventProvider()
        evt = prov.parse({"X-GitHub-Event": "push"}, body)
        assert evt is not None
        assert evt.kind == "push"
        assert evt.repo == "acme/repo"
        assert evt.ref == "refs/heads/main"

    def test_parse_workflow_run_completed_failure(self):
        body = json.dumps(
            {
                "action": "completed",
                "workflow_run": {
                    "conclusion": "failure",
                    "html_url": "https://github.com/acme/repo/actions/runs/1",
                    "head_branch": "main",
                },
                "repository": {"full_name": "acme/repo"},
            }
        ).encode()
        prov = GitHubEventProvider()
        evt = prov.parse({"X-GitHub-Event": "workflow_run"}, body)
        assert evt is not None
        assert evt.kind == "workflow_run.failure"
        assert evt.ref == "main"

    def test_parse_unknown_event_returns_none(self):
        prov = GitHubEventProvider()
        evt = prov.parse({"X-GitHub-Event": "marketplace_purchase"}, b"{}")
        assert evt is None


# ---------------------------------------------------------------------------
# GitLab adapter — token-equality signature
# ---------------------------------------------------------------------------


class TestGitLabProvider:
    def test_verify_token_equality(self):
        prov = GitLabEventProvider()
        assert prov.verify({"X-Gitlab-Token": "secret"}, b"{}", "secret") is True
        assert prov.verify({"X-Gitlab-Token": "wrong"}, b"{}", "secret") is False

    def test_parse_push(self):
        body = json.dumps(
            {
                "object_kind": "push",
                "ref": "refs/heads/main",
                "project": {"path_with_namespace": "acme/repo"},
                "user_username": "@me",
            }
        ).encode()
        prov = GitLabEventProvider()
        evt = prov.parse({"X-Gitlab-Event": "Push Hook"}, body)
        assert evt is not None
        assert evt.kind == "push"
        assert evt.repo == "acme/repo"


# ---------------------------------------------------------------------------
# Top-level receive() — verify + parse + return canonical event
# ---------------------------------------------------------------------------


class TestReceive:
    def test_github_happy_path(self):
        secret = "topsecret"
        body = json.dumps(
            {
                "action": "opened",
                "pull_request": {
                    "number": 1,
                    "head": {"ref": "feat/x"},
                    "html_url": "u",
                    "user": {"login": "@me"},
                },
                "repository": {"full_name": "acme/repo"},
            }
        ).encode()
        headers = {
            "X-GitHub-Event": "pull_request",
            "X-Hub-Signature-256": _gh_sign(secret, body),
        }
        evt = receive(headers, body, secrets={"github": secret})
        assert isinstance(evt, RepoEvent)
        assert evt.kind == "pull_request.opened"

    def test_bad_signature_raises(self):
        with pytest.raises(SignatureMismatch):
            receive(
                {
                    "X-GitHub-Event": "push",
                    "X-Hub-Signature-256": "sha256=" + "0" * 64,
                },
                b"{}",
                secrets={"github": "secret"},
            )

    def test_unknown_provider_returns_none(self):
        evt = receive({"X-Random": "x"}, b"{}", secrets={})
        assert evt is None

    def test_no_secret_configured_skips_verification(self):
        # When secrets dict has no entry for the provider, the receiver
        # accepts the event but flags it (verified=False). This is the
        # "dev mode" path; production callers should always pass secrets.
        body = json.dumps(
            {
                "ref": "refs/heads/main",
                "repository": {"full_name": "acme/repo"},
                "pusher": {"name": "@me"},
                "compare": "u",
            }
        ).encode()
        evt = receive(
            {"X-GitHub-Event": "push"}, body, secrets={}, require_verified=False
        )
        assert evt is not None
        assert evt.verified is False
