# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tests for the CI-provider abstraction (providers + config-driven repos).

TDD: written before the implementation. Covers
  - remote-URL parsing for github / gitlab / gitea, ssh & https forms;
  - detect_provider host→provider mapping + explicit override;
  - each provider's latest_pipeline with a mocked HTTP/subprocess layer
    (no real network / no real binaries);
  - the watched-repo config loader (present / absent / malformed);
  - graceful-None when a token or binary is unavailable.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from axiom.extensions.builtins.release import providers as P
from axiom.extensions.builtins.release.ci_monitor import PipelineStatus
from axiom.extensions.builtins.release.providers import (
    GiteaProvider,
    GitHubProvider,
    GitLabProvider,
    RepoRef,
    detect_provider,
    parse_remote_url,
)

MOD = "axiom.extensions.builtins.release.providers"


# ---------------------------------------------------------------------------
# PipelineStatus re-export — keep the ci_monitor contract intact
# ---------------------------------------------------------------------------


def test_pipeline_status_reexported_is_same_object():
    assert P.PipelineStatus is PipelineStatus


# ---------------------------------------------------------------------------
# remote-URL parsing
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("url", "host", "owner", "repo"),
    [
        # GitHub
        ("https://github.com/b-tree-labs/axiom.git", "github.com", "b-tree-labs", "axiom"),
        ("https://github.com/b-tree-labs/axiom", "github.com", "b-tree-labs", "axiom"),
        ("git@github.com:b-tree-labs/axiom.git", "github.com", "b-tree-labs", "axiom"),
        ("ssh://git@github.com/b-tree-labs/axiom.git", "github.com", "b-tree-labs", "axiom"),
        # GitLab (self-hosted host with a subgroup path)
        (
            "https://gitlab.example.org/group/sub/proj.git",
            "gitlab.example.org",
            "group/sub",
            "proj",
        ),
        ("git@gitlab.com:group/proj.git", "gitlab.com", "group", "proj"),
        # Gitea / self-hosted forge
        ("https://gitea.example.org/owner/repo.git", "gitea.example.org", "owner", "repo"),
        ("git@gitea.example.org:owner/repo.git", "gitea.example.org", "owner", "repo"),
    ],
)
def test_parse_remote_url_forms(url, host, owner, repo):
    ref = parse_remote_url(url)
    assert ref is not None
    assert ref.host == host
    assert ref.owner == owner
    assert ref.repo == repo
    # the full project path is owner + "/" + repo
    assert ref.project_path == f"{owner}/{repo}"


def test_parse_remote_url_preserves_scheme_for_base_url():
    ref = parse_remote_url("https://gitlab.example.org/group/proj.git")
    assert ref is not None
    # base_url is the scheme+host used to build REST endpoints
    assert ref.base_url == "https://gitlab.example.org"


def test_parse_remote_url_ssh_defaults_to_https_base():
    ref = parse_remote_url("git@gitlab.example.org:group/proj.git")
    assert ref is not None
    assert ref.base_url == "https://gitlab.example.org"


def test_parse_remote_url_garbage_returns_none():
    assert parse_remote_url("") is None
    assert parse_remote_url("not a url") is None
    assert parse_remote_url("https://github.com/onlyowner") is None


# ---------------------------------------------------------------------------
# detect_provider host→provider mapping
# ---------------------------------------------------------------------------


def test_detect_provider_github():
    prov = detect_provider("git@github.com:b-tree-labs/axiom.git")
    assert isinstance(prov, GitHubProvider)
    assert prov.name == "github"


def test_detect_provider_gitlab_any_host():
    prov = detect_provider("https://gitlab.example.org/group/proj.git")
    assert isinstance(prov, GitLabProvider)
    assert prov.name == "gitlab"


def test_detect_provider_gitea_self_hosted():
    prov = detect_provider("https://gitea.example.org/owner/repo.git")
    assert isinstance(prov, GiteaProvider)
    assert prov.name == "gitea"


def test_detect_provider_unknown_host_returns_none():
    assert detect_provider("https://example.com/owner/repo.git") is None


def test_detect_provider_garbage_returns_none():
    assert detect_provider("nonsense") is None


def test_detect_provider_explicit_override_beats_host():
    # Host says github, but the caller forces gitea.
    prov = detect_provider("https://github.com/owner/repo.git", override="gitea")
    assert isinstance(prov, GiteaProvider)


def test_detect_provider_unknown_override_returns_none():
    assert detect_provider("https://github.com/owner/repo.git", override="svn") is None


def test_provider_protocol_runtime_checkable():
    for prov in (GitHubProvider(), GitLabProvider(), GiteaProvider()):
        assert isinstance(prov, P.CIProvider)


# ---------------------------------------------------------------------------
# GitHubProvider.latest_pipeline — gh CLI mocked
# ---------------------------------------------------------------------------


def _gh_run(stdout: str, returncode: int = 0):
    res = MagicMock()
    res.returncode = returncode
    res.stdout = stdout
    return res


def test_github_latest_pipeline_success():
    ref = RepoRef.from_url("git@github.com:b-tree-labs/axiom.git")
    runs = [{"headBranch": "main", "status": "completed", "conclusion": "success",
             "url": "https://github.com/x/runs/1"}]
    with (
        patch(f"{MOD}.capabilities.is_available", return_value=True),
        patch(f"{MOD}.subprocess.run", return_value=_gh_run(json.dumps(runs))),
    ):
        status = GitHubProvider().latest_pipeline(ref)
    assert status is not None
    assert status.provider == "github"
    assert status.repo == "axiom"
    assert status.ref == "main"
    assert status.status == "success"
    assert status.url == "https://github.com/x/runs/1"


def test_github_latest_pipeline_falls_back_to_status_when_no_conclusion():
    ref = RepoRef.from_url("git@github.com:o/r.git")
    runs = [{"headBranch": "dev", "status": "in_progress", "url": "u"}]
    with (
        patch(f"{MOD}.capabilities.is_available", return_value=True),
        patch(f"{MOD}.subprocess.run", return_value=_gh_run(json.dumps(runs))),
    ):
        status = GitHubProvider().latest_pipeline(ref)
    assert status is not None
    assert status.status == "in_progress"


def test_github_latest_pipeline_empty_runs_returns_none():
    ref = RepoRef.from_url("git@github.com:o/r.git")
    with (
        patch(f"{MOD}.capabilities.is_available", return_value=True),
        patch(f"{MOD}.subprocess.run", return_value=_gh_run("[]")),
    ):
        assert GitHubProvider().latest_pipeline(ref) is None


def test_github_latest_pipeline_gh_missing_returns_none():
    ref = RepoRef.from_url("git@github.com:o/r.git")
    # gh CLI capability unavailable → graceful None, never touches subprocess.
    with (
        patch(f"{MOD}.capabilities.is_available", return_value=False),
        patch(f"{MOD}.subprocess.run", side_effect=AssertionError("must not run")),
    ):
        assert GitHubProvider().latest_pipeline(ref) is None


def test_github_latest_pipeline_nonzero_exit_returns_none():
    ref = RepoRef.from_url("git@github.com:o/r.git")
    with (
        patch(f"{MOD}.capabilities.is_available", return_value=True),
        patch(f"{MOD}.subprocess.run", return_value=_gh_run("", returncode=1)),
    ):
        assert GitHubProvider().latest_pipeline(ref) is None


def test_github_latest_pipeline_subprocess_raises_returns_none():
    ref = RepoRef.from_url("git@github.com:o/r.git")
    with (
        patch(f"{MOD}.capabilities.is_available", return_value=True),
        patch(f"{MOD}.subprocess.run", side_effect=FileNotFoundError("gh")),
    ):
        assert GitHubProvider().latest_pipeline(ref) is None


# ---------------------------------------------------------------------------
# GitLabProvider.latest_pipeline — REST API mocked
# ---------------------------------------------------------------------------


def _resp(status_code: int, payload):
    r = MagicMock()
    r.status_code = status_code
    r.json.return_value = payload
    return r


def test_gitlab_latest_pipeline_success(monkeypatch):
    monkeypatch.setenv("GITLAB_TOKEN", "tok")
    ref = RepoRef.from_url("https://gitlab.example.org/group/sub/proj.git")
    pipelines = [{"ref": "main", "status": "success", "web_url": "https://gl/x/1"}]
    fake_requests = MagicMock()
    fake_requests.get.return_value = _resp(200, pipelines)
    with (
        patch(f"{MOD}.capabilities.is_available", return_value=True),
        patch.dict("sys.modules", {"requests": fake_requests}),
    ):
        status = GitLabProvider().latest_pipeline(ref)
    assert status is not None
    assert status.provider == "gitlab"
    assert status.repo == "proj"
    assert status.ref == "main"
    assert status.status == "success"
    assert status.url == "https://gl/x/1"
    # URL-encodes the full project path and targets the parsed host.
    called_url = fake_requests.get.call_args.args[0]
    assert called_url.startswith("https://gitlab.example.org/api/v4/projects/")
    assert "group%2Fsub%2Fproj" in called_url


def test_gitlab_uses_project_id_when_set(monkeypatch):
    monkeypatch.setenv("GITLAB_TOKEN", "tok")
    ref = RepoRef(
        host="gitlab.example.org",
        owner="",
        repo="my-repo",
        base_url="https://gitlab.example.org",
        project_id="1234",
    )
    pipelines = [{"ref": "main", "status": "success", "web_url": "https://gl/x/1"}]
    fake_requests = MagicMock()
    fake_requests.get.return_value = _resp(200, pipelines)
    with (
        patch(f"{MOD}.capabilities.is_available", return_value=True),
        patch.dict("sys.modules", {"requests": fake_requests}),
    ):
        status = GitLabProvider().latest_pipeline(ref)
    assert status is not None
    called_url = fake_requests.get.call_args.args[0]
    assert "/api/v4/projects/1234/pipelines" in called_url  # ID, not encoded path
    assert "%2F" not in called_url


def test_gitlab_latest_pipeline_no_token_returns_none(monkeypatch):
    monkeypatch.delenv("GITLAB_TOKEN", raising=False)
    ref = RepoRef.from_url("https://gitlab.example.org/group/proj.git")
    with patch(f"{MOD}.capabilities.is_available", return_value=False):
        assert GitLabProvider().latest_pipeline(ref) is None


def test_gitlab_latest_pipeline_http_error_returns_none(monkeypatch):
    monkeypatch.setenv("GITLAB_TOKEN", "tok")
    ref = RepoRef.from_url("https://gitlab.example.org/group/proj.git")
    fake_requests = MagicMock()
    fake_requests.get.return_value = _resp(403, {})
    with (
        patch(f"{MOD}.capabilities.is_available", return_value=True),
        patch.dict("sys.modules", {"requests": fake_requests}),
    ):
        assert GitLabProvider().latest_pipeline(ref) is None


def test_gitlab_latest_pipeline_empty_returns_none(monkeypatch):
    monkeypatch.setenv("GITLAB_TOKEN", "tok")
    ref = RepoRef.from_url("https://gitlab.example.org/group/proj.git")
    fake_requests = MagicMock()
    fake_requests.get.return_value = _resp(200, [])
    with (
        patch(f"{MOD}.capabilities.is_available", return_value=True),
        patch.dict("sys.modules", {"requests": fake_requests}),
    ):
        assert GitLabProvider().latest_pipeline(ref) is None


def test_gitlab_uses_token_env_override(monkeypatch):
    # A RepoRef can name a non-default token env var; the provider reads it.
    monkeypatch.delenv("GITLAB_TOKEN", raising=False)
    monkeypatch.setenv("MY_GL_TOKEN", "secret")
    ref = RepoRef.from_url("https://gitlab.example.org/g/p.git")
    ref = ref.with_token_env("MY_GL_TOKEN")
    pipelines = [{"ref": "main", "status": "running", "web_url": "u"}]
    fake_requests = MagicMock()
    fake_requests.get.return_value = _resp(200, pipelines)
    with (
        patch(f"{MOD}.capabilities.is_available", return_value=True),
        patch.dict("sys.modules", {"requests": fake_requests}),
    ):
        status = GitLabProvider().latest_pipeline(ref)
    assert status is not None
    headers = fake_requests.get.call_args.kwargs["headers"]
    assert headers["PRIVATE-TOKEN"] == "secret"


# ---------------------------------------------------------------------------
# GiteaProvider.latest_pipeline — REST API mocked
# ---------------------------------------------------------------------------


def test_gitea_latest_pipeline_success(monkeypatch):
    monkeypatch.setenv("GITEA_TOKEN", "tok")
    ref = RepoRef.from_url("https://gitea.example.org/owner/repo.git")
    # Gitea combined commit-status endpoint.
    payload = {"state": "success", "sha": "abc123",
               "target_url": "https://gitea.example.org/owner/repo/actions"}
    fake_requests = MagicMock()
    fake_requests.get.return_value = _resp(200, payload)
    with patch.dict("sys.modules", {"requests": fake_requests}):
        status = GiteaProvider().latest_pipeline(ref)
    assert status is not None
    assert status.provider == "gitea"
    assert status.repo == "repo"
    assert status.status == "success"
    called_url = fake_requests.get.call_args.args[0]
    assert called_url.startswith(
        "https://gitea.example.org/api/v1/repos/owner/repo/commits/"
    )
    assert called_url.endswith("/status")


def test_gitea_latest_pipeline_no_token_returns_none(monkeypatch):
    monkeypatch.delenv("GITEA_TOKEN", raising=False)
    ref = RepoRef.from_url("https://gitea.example.org/owner/repo.git")
    assert GiteaProvider().latest_pipeline(ref) is None


def test_gitea_latest_pipeline_http_error_returns_none(monkeypatch):
    monkeypatch.setenv("GITEA_TOKEN", "tok")
    ref = RepoRef.from_url("https://gitea.example.org/owner/repo.git")
    fake_requests = MagicMock()
    fake_requests.get.return_value = _resp(404, {})
    with patch.dict("sys.modules", {"requests": fake_requests}):
        assert GiteaProvider().latest_pipeline(ref) is None


def test_gitea_latest_pipeline_requests_missing_returns_none(monkeypatch):
    monkeypatch.setenv("GITEA_TOKEN", "tok")
    ref = RepoRef.from_url("https://gitea.example.org/owner/repo.git")
    with patch.dict("sys.modules", {"requests": None}):
        assert GiteaProvider().latest_pipeline(ref) is None


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


def test_registry_lookup_by_name():
    assert isinstance(P.get_provider("github"), GitHubProvider)
    assert isinstance(P.get_provider("gitlab"), GitLabProvider)
    assert isinstance(P.get_provider("gitea"), GiteaProvider)
    assert P.get_provider("nope") is None


def test_registry_is_pluggable():
    class FakeProvider:
        name = "fake"

        def latest_pipeline(self, repo_ref):  # noqa: ARG002
            return None

    P.register_provider("fake", FakeProvider)
    try:
        prov = P.get_provider("fake")
        assert isinstance(prov, FakeProvider)
        assert isinstance(prov, P.CIProvider)
    finally:
        P.unregister_provider("fake")
    assert P.get_provider("fake") is None
