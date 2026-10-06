# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tests for the config-driven watched-repo list + check_pipelines() wiring.

TDD: written before the implementation. Covers
  - the watched-repo config loader (present / absent / malformed);
  - default Axiom GitHub entry when no config exists;
  - explicit remote-URL entries (auto-detect) and structured entries;
  - check_pipelines() iterating config + skipping unavailable providers
    gracefully (never crashing);
  - NO domain-consumer endpoint (rsicc / tacc / project 77) baked in.
"""

from __future__ import annotations

import inspect
from unittest.mock import patch

from axiom.extensions.builtins.release import ci_monitor, providers
from axiom.extensions.builtins.release.ci_monitor import (
    PipelineStatus,
    check_pipelines,
    load_watched_repos,
)

MON = "axiom.extensions.builtins.release.ci_monitor"


# ---------------------------------------------------------------------------
# Layering invariant — no domain consumer named anywhere in the module
# ---------------------------------------------------------------------------


def test_no_domain_consumer_strings_in_source():
    src = inspect.getsource(ci_monitor).lower()
    for forbidden in ("rsicc", "tacc", "utexas", "neutron", "nuclear", "/projects/77"):
        assert forbidden not in src, f"domain-consumer leak: {forbidden!r}"


# ---------------------------------------------------------------------------
# Config loader
# ---------------------------------------------------------------------------


def test_load_watched_repos_absent_returns_default(tmp_path, monkeypatch):
    # No config file → Axiom's own GitHub repo as a sensible default.
    monkeypatch.setattr(f"{MON}._ci_repos_config_path", lambda: tmp_path / "missing.toml")
    monkeypatch.delenv("AXI_CI_REPOS", raising=False)
    repos = load_watched_repos()
    assert len(repos) == 1
    assert repos[0]["provider"] == "github"
    # The default must not name any domain consumer.
    blob = str(repos).lower()
    for forbidden in ("rsicc", "tacc", "neutron", "/projects/77"):
        assert forbidden not in blob


def test_load_watched_repos_present(tmp_path, monkeypatch):
    cfg = tmp_path / "ci-repos.toml"
    cfg.write_text(
        '[[repo]]\n'
        'provider = "gitlab"\n'
        'host = "gitlab.example.org"\n'
        'project = "group/proj"\n'
        'token_env = "MY_TOKEN"\n'
        '\n'
        '[[repo]]\n'
        'url = "git@github.com:b-tree-labs/axiom.git"\n'
    )
    monkeypatch.setattr(f"{MON}._ci_repos_config_path", lambda: cfg)
    monkeypatch.delenv("AXI_CI_REPOS", raising=False)
    repos = load_watched_repos()
    assert len(repos) == 2
    assert repos[0]["provider"] == "gitlab"
    assert repos[0]["host"] == "gitlab.example.org"
    assert repos[0]["project"] == "group/proj"
    assert repos[0]["token_env"] == "MY_TOKEN"
    assert repos[1]["url"].endswith("axiom.git")


def test_load_watched_repos_env_pointer(tmp_path, monkeypatch):
    cfg = tmp_path / "elsewhere.toml"
    cfg.write_text('[[repo]]\nurl = "https://gitea.example.org/o/r.git"\n')
    # AXI_CI_REPOS points at an explicit file, overriding the default path.
    monkeypatch.setattr(f"{MON}._ci_repos_config_path", lambda: tmp_path / "default.toml")
    monkeypatch.setenv("AXI_CI_REPOS", str(cfg))
    repos = load_watched_repos()
    assert len(repos) == 1
    assert repos[0]["url"].endswith("r.git")


def test_load_watched_repos_malformed_returns_default(tmp_path, monkeypatch):
    cfg = tmp_path / "ci-repos.toml"
    cfg.write_text("this is = = not valid toml [[[")
    monkeypatch.setattr(f"{MON}._ci_repos_config_path", lambda: cfg)
    monkeypatch.delenv("AXI_CI_REPOS", raising=False)
    repos = load_watched_repos()
    # Malformed → fall back to the default entry, never crash.
    assert len(repos) == 1
    assert repos[0]["provider"] == "github"


def test_load_watched_repos_empty_table_returns_default(tmp_path, monkeypatch):
    cfg = tmp_path / "ci-repos.toml"
    cfg.write_text("# valid toml, but no [[repo]] entries\n")
    monkeypatch.setattr(f"{MON}._ci_repos_config_path", lambda: cfg)
    monkeypatch.delenv("AXI_CI_REPOS", raising=False)
    repos = load_watched_repos()
    assert len(repos) == 1
    assert repos[0]["provider"] == "github"


# ---------------------------------------------------------------------------
# check_pipelines() — config-driven iteration
# ---------------------------------------------------------------------------


def test_check_pipelines_reads_config_and_collects(monkeypatch):
    fake_repos = [
        {"url": "git@github.com:o/a.git"},
        {"provider": "gitlab", "host": "gitlab.example.org", "project": "g/p"},
    ]
    monkeypatch.setattr(f"{MON}.load_watched_repos", lambda: fake_repos)

    gh = PipelineStatus(repo="a", provider="github", ref="main", status="success")
    gl = PipelineStatus(repo="p", provider="gitlab", ref="main", status="failed")

    def fake_status_for(entry):
        return gh if entry.get("url") else gl

    monkeypatch.setattr(f"{MON}._status_for_entry", fake_status_for)
    results = check_pipelines()
    assert results == [gh, gl]


def test_check_pipelines_skips_none_entries(monkeypatch):
    fake_repos = [{"url": "git@github.com:o/a.git"}, {"provider": "gitea"}]
    monkeypatch.setattr(f"{MON}.load_watched_repos", lambda: fake_repos)
    gh = PipelineStatus(repo="a", provider="github", ref="main", status="success")
    monkeypatch.setattr(
        f"{MON}._status_for_entry",
        lambda e: gh if e.get("url") else None,
    )
    results = check_pipelines()
    assert results == [gh]


def test_check_pipelines_never_crashes_on_entry_error(monkeypatch):
    fake_repos = [{"url": "git@github.com:o/a.git"}, {"bogus": True}]
    monkeypatch.setattr(f"{MON}.load_watched_repos", lambda: fake_repos)

    def boom(entry):
        if entry.get("bogus"):
            raise RuntimeError("provider exploded")
        return PipelineStatus(repo="a", provider="github", ref="m", status="success")

    monkeypatch.setattr(f"{MON}._status_for_entry", boom)
    # One entry raises; check_pipelines must swallow it and return the rest.
    results = check_pipelines()
    assert len(results) == 1
    assert results[0].repo == "a"


# ---------------------------------------------------------------------------
# _status_for_entry — wiring config entry → provider
# ---------------------------------------------------------------------------


def test_status_for_entry_url_autodetect(monkeypatch):
    entry = {"url": "git@github.com:b-tree-labs/axiom.git"}
    expected = PipelineStatus(repo="axiom", provider="github", ref="main", status="success")
    with patch.object(providers.GitHubProvider, "latest_pipeline", return_value=expected):
        status = ci_monitor._status_for_entry(entry)
    assert status is expected


def test_status_for_entry_structured_gitlab(monkeypatch):
    entry = {
        "provider": "gitlab",
        "host": "gitlab.example.org",
        "project": "group/proj",
        "token_env": "MY_TOKEN",
    }
    expected = PipelineStatus(repo="proj", provider="gitlab", ref="main", status="success")
    with patch.object(providers.GitLabProvider, "latest_pipeline", return_value=expected):
        status = ci_monitor._status_for_entry(entry)
    assert status is expected


def test_repo_ref_from_entry_gitlab_project_id():
    # Numeric project_id addressing (restores parity with old project-ID code).
    entry = {
        "provider": "gitlab",
        "host": "gitlab.example.org",
        "project_id": 1234,  # TOML int → coerced to str
        "name": "my-repo",
        "token_env": "MY_TOKEN",
    }
    ref, provider_name = ci_monitor._repo_ref_from_entry(entry)
    assert provider_name == "gitlab"
    assert ref is not None
    assert ref.project_id == "1234"
    assert ref.repo == "my-repo"
    assert ref.host == "gitlab.example.org"
    assert ref.token_env == "MY_TOKEN"


def test_repo_ref_from_entry_project_id_default_name():
    ref, _ = ci_monitor._repo_ref_from_entry(
        {"provider": "gitlab", "host": "h", "project_id": "1234"}
    )
    assert ref is not None
    assert ref.project_id == "1234"
    assert ref.repo == "project-1234"


def test_status_for_entry_unknown_provider_returns_none():
    assert ci_monitor._status_for_entry({"provider": "svn", "host": "h", "project": "p"}) is None


def test_status_for_entry_empty_returns_none():
    assert ci_monitor._status_for_entry({}) is None
