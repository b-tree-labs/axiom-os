# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Dist-aware Actions storage policy and merged-PR cache selection."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from axiom.extensions.builtins.hygiene.artifact_cleanup import (
    Artifact,
    CacheRecord,
    StoragePolicy,
    _is_version_tag,
)


NOW = datetime(2026, 11, 21, 14, 40, tzinfo=timezone.utc)


def _art(
    id: str,
    *,
    name: str = "dist",
    days_ago: float = 0,
    hours_ago: float | None = None,
    branch: str = "main",
    repo: str = "b-tree-labs/axiom-os-private",
    size: int = 36 * 1024 * 1024,
) -> Artifact:
    delta = timedelta(hours=hours_ago) if hours_ago is not None else timedelta(days=days_ago)
    return Artifact(
        id=id,
        name=name,
        size_bytes=size,
        created_at=NOW - delta,
        workflow_run_id=f"run-{id}",
        provider="github",
        repo=repo,
        head_branch=branch,
    )


def _cache(id: str, ref: str, *, repo: str = "b-tree-labs/axiom-os-private", size: int = 100) -> CacheRecord:
    return CacheRecord(
        id=id,
        key=f"venv-{id}",
        ref=ref,
        size_bytes=size,
        created_at=NOW,
        provider="github",
        repo=repo,
    )


class TestVersionTag:
    def test_version_tags(self):
        assert _is_version_tag("v0.70.43")
        assert _is_version_tag("refs/tags/v1.0.0")
        assert not _is_version_tag("main")
        assert not _is_version_tag("feat/vnext")
        assert not _is_version_tag("vnext")


class TestStoragePolicyArtifacts:
    def test_keeps_newest_two_main_dists_and_deletes_only_those_past_retention(self):
        arts = [
            _art("d0", hours_ago=1),
            _art("d1", hours_ago=2),
            _art("d2", days_ago=2),       # within 3-day window, not newest 2, still kept
            _art("d3", days_ago=4),       # past main_dist_days, deleted
        ]
        to_del, to_keep = StoragePolicy().select_artifacts(arts, NOW)
        assert {a.id for a in to_keep} == {"d0", "d1", "d2"}
        assert {a.id for a in to_del} == {"d3"}

    def test_keeps_latest_tag_dist_and_recent_older_tags(self):
        arts = [
            _art("latest", branch="v0.70.43", hours_ago=10),
            _art("latest-b", branch="v0.70.43", hours_ago=10.1),
            _art("old", branch="v0.70.42", hours_ago=20),       # within 14d, kept
            _art("older", branch="v0.70.41", days_ago=16),      # past release_dist_days
        ]
        to_del, to_keep = StoragePolicy().select_artifacts(arts, NOW)
        assert {a.id for a in to_keep} == {"latest", "latest-b", "old"}
        assert {a.id for a in to_del} == {"older"}

    def test_keeps_recent_logs_and_memory_benchmarks(self):
        arts = [
            _art("log-new", name="test-results-3.13", hours_ago=12, branch="main"),
            _art("log-mid", name="test-results-3.13", days_ago=10, branch="feat/x"),
            _art("log-old", name="test-results-3.13", days_ago=16, branch="feat/x"),
            _art("bench", name="memory-benchmarks-1", days_ago=40, branch="main"),
        ]
        to_del, to_keep = StoragePolicy().select_artifacts(arts, NOW)
        assert {a.id for a in to_keep} == {"log-new", "log-mid", "bench"}
        assert {a.id for a in to_del} == {"log-old"}

    def test_recent_non_dist_not_deleted(self):
        arts = [_art("semgrep", name="semgrep-results", hours_ago=3, branch="main")]
        to_del, to_keep = StoragePolicy().select_artifacts(arts, NOW)
        assert to_del == []
        assert [a.id for a in to_keep] == ["semgrep"]



    def test_pr_dist_keeps_three_days(self):
        arts = [
            _art("pr-new", branch="feat/x", days_ago=1),
            _art("pr-old", branch="feat/x", days_ago=4),
        ]
        to_del, to_keep = StoragePolicy().select_artifacts(arts, NOW)
        assert {a.id for a in to_keep} == {"pr-new"}
        assert {a.id for a in to_del} == {"pr-old"}

    def test_coverage_follows_coverage_days(self):
        arts = [
            _art("cov-new", name="coverage-html", days_ago=2),
            _art("cov-old", name="coverage-xml", days_ago=16),
        ]
        to_del, to_keep = StoragePolicy().select_artifacts(arts, NOW)
        assert {a.id for a in to_keep} == {"cov-new"}
        assert {a.id for a in to_del} == {"cov-old"}

    def test_rivet_keep_skips_even_past_retention(self):
        from axiom.infra.artifact_verdicts import ArtifactVerdict, VerdictLoad

        arts = [_art("old", days_ago=4)]
        verdicts = VerdictLoad(items=[
            ArtifactVerdict.make("keep", "pr_in_flight", repo=arts[0].repo, artifact_id="old", now=NOW),
        ])
        to_del, to_keep = StoragePolicy().select_artifacts(arts, NOW, verdicts=verdicts)
        assert {a.id for a in to_keep} == {"old"}
        assert to_del == []

    def test_rivet_disposable_deletes_early_but_never_touch_wins(self):
        from axiom.infra.artifact_verdicts import ArtifactVerdict, VerdictLoad

        arts = [
            _art("newest", hours_ago=1),
            _art("second", hours_ago=2),
            _art("fresh", hours_ago=3),  # within 3d, but disposable -> early delete
        ]
        verdicts = VerdictLoad(items=[
            ArtifactVerdict.make("disposable", "superseded_green", repo=arts[0].repo, artifact_id="fresh", now=NOW),
            ArtifactVerdict.make("disposable", "superseded_green", repo=arts[0].repo, artifact_id="newest", now=NOW),
        ])
        to_del, to_keep = StoragePolicy().select_artifacts(arts, NOW, verdicts=verdicts)
        assert {a.id for a in to_keep} == {"newest", "second"}  # newest 2 are never-touch
        assert {a.id for a in to_del} == {"fresh"}

    def test_unreachable_verdict_store_never_deletes_early(self):
        from axiom.infra.artifact_verdicts import ArtifactVerdict, VerdictLoad

        arts = [_art("fresh", hours_ago=3)]
        verdicts = VerdictLoad(
            items=[ArtifactVerdict.make("disposable", "superseded_green", repo=arts[0].repo, artifact_id="fresh", now=NOW)],
            unreachable=True,
        )
        to_del, to_keep = StoragePolicy().select_artifacts(arts, NOW, verdicts=verdicts)
        assert to_del == []
        assert {a.id for a in to_keep} == {"fresh"}


class TestStoragePolicyCaches:
    def test_deletes_merged_pr_caches_keeps_main_and_open(self):
        repo = "b-tree-labs/axiom-os-private"
        caches = [
            _cache("main", "refs/heads/main"),
            _cache("open", "refs/pull/311/merge"),
            _cache("merged", "refs/pull/2165/merge"),
            _cache("unknown", "refs/pull/9999/merge"),
            _cache("other", "refs/heads/feat/x"),
        ]
        to_del, to_keep = StoragePolicy().select_caches(
            caches,
            open_prs={repo: {311}},
            closed_or_merged={(repo, 2165): True},
        )
        assert {c.id for c in to_del} == {"merged"}
        assert {c.id for c in to_keep} == {"main", "open", "unknown", "other"}
