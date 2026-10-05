# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tests for ``cross_repo_pr_watch`` — RIVET fans out trunk checks across
a configurable repo list, provider-agnostic."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path


from axiom.extensions.builtins.release.cross_repo_pr_watch import (
    WatchedRepo,
    load_watched_repos,
    cross_repo_pr_watch,
)
from axiom.extensions.builtins.release.pipeline_status import PipelineStatus
from axiom.extensions.builtins.release.providers import RepoRef


NOW = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Stub CI provider — deterministic latest_pipeline per repo
# ---------------------------------------------------------------------------


class StubCIProvider:
    name = "stub"

    def __init__(self, by_repo: dict[str, PipelineStatus | None]):
        self._by_repo = by_repo

    def latest_pipeline(self, repo_ref: RepoRef) -> PipelineStatus | None:
        return self._by_repo.get(repo_ref.project_path)


def _status(state: str, repo: str, ref: str = "main") -> PipelineStatus:
    return PipelineStatus(
        repo=repo,
        provider="stub",
        ref=ref,
        status=state,
        url=f"https://example.test/{repo}/runs/1",
    )


# ---------------------------------------------------------------------------
# Config loading
# ---------------------------------------------------------------------------


class TestLoadWatchedRepos:
    def test_missing_file_returns_empty(self, tmp_path: Path):
        assert load_watched_repos(tmp_path / "nope.toml") == []

    def test_parses_toml_list(self, tmp_path: Path):
        p = tmp_path / "watched.toml"
        p.write_text(
            """
[[repo]]
url = "https://github.com/b-tree-labs/axiom-os"
ref = "main"

[[repo]]
url = "https://github.com/b-tree-labs/classroom-analytics"
ref = "main"
"""
        )
        repos = load_watched_repos(p)
        assert len(repos) == 2
        assert repos[0].repo_ref.owner == "b-tree-labs"
        assert repos[0].repo_ref.repo == "axiom-os"
        assert repos[1].repo_ref.repo == "classroom-analytics"

    def test_skips_unparseable_url(self, tmp_path: Path):
        p = tmp_path / "watched.toml"
        p.write_text(
            """
[[repo]]
url = "not-a-url"

[[repo]]
url = "https://github.com/b-tree-labs/axiom-os"
"""
        )
        repos = load_watched_repos(p)
        assert len(repos) == 1
        assert repos[0].repo_ref.repo == "axiom-os"


# ---------------------------------------------------------------------------
# Fan-out behavior
# ---------------------------------------------------------------------------


class TestCrossRepoFanOut:
    def _targets(self) -> list[WatchedRepo]:
        return [
            WatchedRepo(
                repo_ref=RepoRef(
                    host="github.com",
                    owner="b-tree-labs",
                    repo="axiom-os",
                    base_url="https://github.com",
                    branch="main",
                ),
                ref="main",
            ),
            WatchedRepo(
                repo_ref=RepoRef(
                    host="github.com",
                    owner="b-tree-labs",
                    repo="classroom-analytics",
                    base_url="https://github.com",
                    branch="main",
                ),
                ref="main",
            ),
        ]

    def test_all_green_emits_no_findings(self, tmp_path: Path):
        provider = StubCIProvider(
            {
                "b-tree-labs/axiom-os": _status("success", "axiom-os"),
                "b-tree-labs/classroom-analytics": _status("success", "classroom-analytics"),
            }
        )
        findings, snaps = cross_repo_pr_watch(
            self._targets(),
            state_dir=tmp_path,
            provider=provider,
            now=NOW,
        )
        assert findings == []
        assert len(snaps) == 2

    def test_one_red_emits_finding_for_that_repo_only(self, tmp_path: Path):
        provider = StubCIProvider(
            {
                "b-tree-labs/axiom-os": _status("success", "axiom-os"),
                "b-tree-labs/classroom-analytics": _status("failure", "classroom-analytics"),
            }
        )
        findings, _ = cross_repo_pr_watch(
            self._targets(),
            state_dir=tmp_path,
            provider=provider,
            now=NOW,
        )
        assert len(findings) == 1
        assert findings[0].repo.endswith("classroom-analytics")
        assert findings[0].severity == "red_first_tick"

    def test_missing_status_is_skipped_not_crashed(self, tmp_path: Path):
        provider = StubCIProvider(
            {"b-tree-labs/axiom-os": _status("success", "axiom-os")}
        )  # classroom-analytics returns None
        findings, snaps = cross_repo_pr_watch(
            self._targets(),
            state_dir=tmp_path,
            provider=provider,
            now=NOW,
        )
        assert findings == []
        # Only the repo we got a status for shows up in snapshots.
        assert len(snaps) == 1

    def test_state_persists_across_calls(self, tmp_path: Path):
        # First call: red on classroom-analytics → first_tick finding.
        red = StubCIProvider(
            {
                "b-tree-labs/axiom-os": _status("success", "axiom-os"),
                "b-tree-labs/classroom-analytics": _status("failure", "classroom-analytics"),
            }
        )
        findings_1, _ = cross_repo_pr_watch(
            self._targets(), state_dir=tmp_path, provider=red, now=NOW
        )
        assert len(findings_1) == 1
        # Second call: same red → no first_tick (state remembers it).
        findings_2, _ = cross_repo_pr_watch(
            self._targets(), state_dir=tmp_path, provider=red, now=NOW
        )
        assert all(f.severity != "red_first_tick" for f in findings_2)


# ---------------------------------------------------------------------------
# Regression: this exact gap caused the 28-hour silent classroom-analytics red CI
# ---------------------------------------------------------------------------


class TestDownstreamSilenceRegression:
    """RIVET was watching only its own repo; a downstream consumer's main
    can go red and stay there unnoticed. With cross_repo_pr_watch + a
    config listing that repo, the first poll surfaces it."""

    def test_downstream_red_surfaces_on_first_tick(self, tmp_path: Path):
        targets = [
            WatchedRepo(
                repo_ref=RepoRef(
                    host="github.com",
                    owner="b-tree-labs",
                    repo="classroom-analytics",
                    base_url="https://github.com",
                    branch="main",
                ),
                ref="main",
            )
        ]
        provider = StubCIProvider(
            {"b-tree-labs/classroom-analytics": _status("failure", "classroom-analytics")}
        )
        findings, _ = cross_repo_pr_watch(
            targets, state_dir=tmp_path, provider=provider, now=NOW
        )
        assert len(findings) == 1
        assert "classroom-analytics" in findings[0].repo
        assert findings[0].severity == "red_first_tick"
