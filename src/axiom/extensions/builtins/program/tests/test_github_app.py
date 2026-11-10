# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""GitHub App auth and the REST client — no network, no ``gh`` CLI.

A fake transport stands in for HTTPS. The tests pin: the App JWT is signed
with the vault key and names the App; the installation token asks for the
read-only permissions and is cached until near expiry; an installation can be
found by account; the client reads every page and never shells out to ``gh``;
and neither the key nor a token ever appears in a repr, an error, or a log.
"""

from __future__ import annotations

import json
import logging

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from axiom.extensions.builtins.program.skills._clients import GitHubCliClient, TrackerReadError
from axiom.extensions.builtins.program.skills._github_app import (
    READ_PERMISSIONS,
    GitHubAppConfig,
    GitHubAppError,
    GitHubAppTokenProvider,
    GitHubRestClient,
    HttpResponse,
    build_github_client,
)

TOKEN = "ghs_fake_installation_token_value"


@pytest.fixture(scope="module")
def keypair():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    return pem, key.public_key()


class FakeTransport:
    """Routes (method, path) to canned responses and records every call."""

    def __init__(self, routes=None, *, expires_at="2099-01-01T00:00:00Z"):
        self.calls = []
        self.routes = routes or {}
        self.expires_at = expires_at

    def __call__(self, method, url, headers, body):
        self.calls.append({"method": method, "url": url, "headers": headers, "body": body})
        path = url.split("://", 1)[1].split("/", 1)[1]
        if method == "POST" and path.endswith("/access_tokens"):
            return HttpResponse(201, {}, json.dumps({"token": TOKEN, "expires_at": self.expires_at}).encode())
        for (m, prefix), response in self.routes.items():
            if m == method and path.startswith(prefix):
                return response(path) if callable(response) else response
        return HttpResponse(404)

    def token_requests(self):
        return [c for c in self.calls if c["method"] == "POST"]


def _config(**over):
    raw = {"app_id": 12345, "installation_id": 67890, "credential": "example-app-key", **over}
    return GitHubAppConfig.from_raw(raw)


def _provider(keypair, transport, **kw):
    pem, _ = keypair
    return GitHubAppTokenProvider(_config(), key_loader=lambda: pem, transport=transport, **kw)


class TestConfig:
    def test_needs_app_id_credential_and_an_installation_or_org(self):
        assert GitHubAppConfig.from_raw({"app_id": 1, "credential": "k"}) is None
        assert GitHubAppConfig.from_raw({"app_id": 1, "installation_id": 2}) is None
        assert GitHubAppConfig.from_raw({"app_id": 1, "credential": "k", "org": "example-org"}).org == "example-org"
        assert GitHubAppConfig.from_raw(None) is None


class TestInstallationToken:
    def test_jwt_is_signed_with_the_key_and_names_the_app(self, keypair):
        transport = FakeTransport()
        assert _provider(keypair, transport).token() == TOKEN
        auth = transport.token_requests()[0]["headers"]["Authorization"]
        claims = jwt.decode(auth.split(" ", 1)[1], keypair[1], algorithms=["RS256"])
        assert claims["iss"] == "12345"
        assert claims["exp"] - claims["iat"] <= 600

    def test_requests_read_only_permissions_for_the_installation(self, keypair):
        transport = FakeTransport()
        _provider(keypair, transport).token()
        call = transport.token_requests()[0]
        assert call["url"].endswith("/app/installations/67890/access_tokens")
        assert json.loads(call["body"]) == {"permissions": READ_PERMISSIONS}
        assert set(READ_PERMISSIONS.values()) == {"read"}
        assert set(READ_PERMISSIONS) == {"issues", "pull_requests", "contents", "metadata"}

    def test_a_narrowed_token_names_its_permissions_and_repositories(self, keypair):
        transport = FakeTransport()
        _provider(keypair, transport, permissions={"contents": "write"}, repositories=["tracker"]).token()
        body = json.loads(transport.token_requests()[0]["body"])
        assert body == {"permissions": {"contents": "write"}, "repositories": ["tracker"]}

    def test_cached_until_near_expiry(self, keypair):
        from datetime import UTC, datetime

        transport = FakeTransport(expires_at="2026-11-09T01:00:00Z")
        expiry = datetime(2026, 11, 9, 1, tzinfo=UTC).timestamp()
        clock = {"t": expiry - 3600}  # an hour before expiry
        provider = _provider(keypair, transport, clock=lambda: clock["t"])
        provider.token()
        provider.token()
        assert len(transport.token_requests()) == 1
        clock["t"] = expiry - 120  # within the refresh margin
        provider.token()
        assert len(transport.token_requests()) == 2

    def test_installation_found_by_org(self, keypair):
        pem, _ = keypair
        transport = FakeTransport(
            {("GET", "orgs/example-org/installation"): HttpResponse(200, {}, b'{"id": 555}')}
        )
        config = GitHubAppConfig.from_raw({"app_id": 1, "org": "example-org", "credential": "k"})
        GitHubAppTokenProvider(config, key_loader=lambda: pem, transport=transport).token()
        assert transport.token_requests()[0]["url"].endswith("/app/installations/555/access_tokens")

    def test_a_refusal_names_the_status_never_a_secret(self, keypair, caplog):
        pem, _ = keypair

        def refuse(method, url, headers, body):
            return HttpResponse(401, {}, b'{"message": "Bad credentials"}')

        provider = GitHubAppTokenProvider(_config(), key_loader=lambda: pem, transport=refuse)
        caplog.set_level(logging.DEBUG)
        with pytest.raises(GitHubAppError) as exc:
            provider.token()
        assert "401" in str(exc.value)
        assert "PRIVATE KEY" not in str(exc.value)
        assert "PRIVATE KEY" not in caplog.text

    def test_a_bad_key_does_not_echo_the_key(self):
        provider = GitHubAppTokenProvider(_config(), key_loader=lambda: "not-a-key-SECRETVALUE", transport=FakeTransport())
        with pytest.raises(GitHubAppError) as exc:
            provider.token()
        assert "SECRETVALUE" not in str(exc.value)

    def test_repr_redacts(self, keypair):
        provider = _provider(keypair, FakeTransport())
        provider.token()
        assert TOKEN not in repr(provider)
        client = GitHubRestClient(repo="example-org/program-tracker", tokens=provider)
        assert TOKEN not in repr(client)


def _page(items, next_url=None):
    headers = {"link": f'<{next_url}>; rel="next"'} if next_url else {}
    return HttpResponse(200, headers, json.dumps(items).encode())


class TestRestClient:
    def _client(self, keypair, routes):
        transport = FakeTransport(routes)
        return GitHubRestClient(repo="example-org/program-tracker", tokens=_provider(keypair, transport)), transport

    def test_issues_read_every_page_skip_prs_and_carry_the_crosslink(self, keypair, monkeypatch):
        import subprocess

        def no_gh(*a, **k):
            raise AssertionError("the App client must not shell out")

        monkeypatch.setattr(subprocess, "run", no_gh)

        def issues(path):
            if "page=2" in path:
                return _page([{"number": 2, "title": "b", "state": "closed", "updated_at": "2026-11-02T00:00:00Z"}])
            return _page(
                [
                    {
                        "number": 1,
                        "title": "a",
                        "state": "open",
                        "body": "Scope text.\n\nGitLab tracker: https://gitlab.example.org/group/proj/-/issues/9",
                        "updated_at": "2026-11-01T00:00:00Z",
                    },
                    {"number": 3, "pull_request": {}, "title": "pr"},
                ],
                next_url="https://api.github.com/repos/example-org/program-tracker/issues?page=2",
            )

        client, transport = self._client(keypair, {("GET", "repos/example-org/program-tracker/issues"): issues})
        got = client.issues(None)
        assert [i.ref for i in got] == [1, 2]
        assert got[0].crosslink == "https://gitlab.example.org/group/proj/-/issues/9"
        assert got[1].crosslink is None
        reads = [c for c in transport.calls if c["method"] == "GET"]
        assert all(c["headers"]["Authorization"] == f"Bearer {TOKEN}" for c in reads)

    def test_a_failed_page_raises_never_a_silent_prefix(self, keypair):
        def issues(path):
            if "page=2" in path:
                return HttpResponse(502)
            return _page([{"number": 1}], next_url="https://api.github.com/repos/example-org/program-tracker/issues?page=2")

        client, _ = self._client(keypair, {("GET", "repos/example-org/program-tracker/issues"): issues})
        with pytest.raises(TrackerReadError):
            client.issues(None)

    def test_readiness_rungs(self, keypair):
        client, _ = self._client(
            keypair,
            {
                ("GET", "rate_limit"): HttpResponse(200, {}, b"{}"),
                ("GET", "repos/example-org/program-tracker"): HttpResponse(200, {}, b"{}"),
            },
        )
        assert client.ping() is True
        assert client.whoami() == "app:12345"
        assert client.project_readable() is True

    def test_issue_readable_is_tri_state(self, keypair):
        client, _ = self._client(
            keypair,
            {
                ("GET", "repos/example-org/program-tracker/issues/1"): HttpResponse(200, {}, b"{}"),
                ("GET", "repos/example-org/program-tracker/issues/2"): HttpResponse(404),
                ("GET", "repos/example-org/program-tracker/issues/3"): HttpResponse(500),
            },
        )
        assert client.issue_readable(1) is True
        assert client.issue_readable(2) is False
        assert client.issue_readable(3) is None

    def test_work_items_normalize_issues_and_merged_prs(self, keypair):
        rows = [
            {"number": 5, "state": "closed", "html_url": "https://github.com/example-org/svc/issues/5",
             "title": "fix", "body": "program-tracker#1", "closed_at": "2026-11-03T00:00:00Z",
             "updated_at": "2026-11-03T00:00:00Z"},
            {"number": 6, "state": "closed", "html_url": "https://github.com/example-org/svc/pull/6",
             "pull_request": {"merged_at": "2026-11-04T00:00:00Z"}, "title": "impl",
             "updated_at": "2026-11-04T00:00:00Z"},
        ]
        client, _ = self._client(keypair, {("GET", "repos/example-org/svc/issues"): _page(rows)})
        got = client.work_items("example-org/svc", "2026-11-01T00:00:00Z")
        assert [(w.kind, w.state, w.done_at) for w in got] == [
            ("issue", "closed", "2026-11-03T00:00:00Z"),
            ("pr", "merged", "2026-11-04T00:00:00Z"),
        ]

    def test_org_repos_filters_to_the_account(self, keypair):
        body = {"repositories": [
            {"full_name": "example-org/a", "owner": {"login": "example-org"}, "topics": ["program"]},
            {"full_name": "other-org/b", "owner": {"login": "other-org"}},
        ]}
        client, _ = self._client(
            keypair, {("GET", "installation/repositories"): HttpResponse(200, {}, json.dumps(body).encode())}
        )
        assert [r["full_name"] for r in client.org_repos("example-org")] == ["example-org/a"]


class TestChoosingTheClient:
    def test_no_app_declared_keeps_the_gh_cli_fallback(self, tmp_path):
        client = build_github_client({}, tmp_path, repo="example-org/program-tracker")
        assert isinstance(client, GitHubCliClient)

    def test_an_app_declared_builds_the_rest_client(self, tmp_path):
        program = {"github_app": {"app_id": 1, "installation_id": 2, "credential": "example-app-key"}}
        client = build_github_client(program, tmp_path, repo="example-org/program-tracker")
        assert isinstance(client, GitHubRestClient)
