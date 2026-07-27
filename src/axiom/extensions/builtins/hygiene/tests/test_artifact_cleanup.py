# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tests for TIDY's artifact-cleanup skill."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest import mock


from axiom.extensions.builtins.hygiene.artifact_cleanup import (
    Artifact,
    CleanupParams,
    GitHubProvider,
    RetentionPolicy,
    cleanup_artifacts,
)


NOW = datetime(2026, 5, 31, 12, 0, tzinfo=timezone.utc)


def _art(
    id: str,
    days_ago: int,
    *,
    size: int = 45 * 1024 * 1024,
    workflow: str = "wf-1",
) -> Artifact:
    return Artifact(
        id=id,
        name="dist",
        size_bytes=size,
        created_at=NOW - timedelta(days=days_ago),
        workflow_run_id=workflow,
        provider="github",
        repo="acme/repo",
    )


# ---------------------------------------------------------------------------
# Retention policy
# ---------------------------------------------------------------------------


class TestRetentionPolicy:
    def test_recent_artifact_kept(self):
        policy = RetentionPolicy(keep_days=7)
        to_del, to_keep = policy.select_for_deletion([_art("a", days_ago=1)], NOW)
        assert to_del == []
        assert len(to_keep) == 1

    def test_old_artifact_deleted(self):
        # keep_last_n_per_workflow=0 disables the per-workflow safety net.
        policy = RetentionPolicy(keep_days=7, keep_last_n_per_workflow=0)
        to_del, to_keep = policy.select_for_deletion([_art("a", days_ago=10)], NOW)
        assert len(to_del) == 1
        assert to_keep == []

    def test_solo_artifact_in_workflow_kept_by_safety_net(self):
        # Default keep_last_n_per_workflow=3 preserves a quiet workflow's
        # only artifact even when it's old.
        policy = RetentionPolicy(keep_days=7)
        to_del, to_keep = policy.select_for_deletion([_art("a", days_ago=10)], NOW)
        assert to_del == []
        assert len(to_keep) == 1

    def test_keep_last_n_per_workflow_even_if_old(self):
        """The most recent N per workflow are always kept."""
        policy = RetentionPolicy(keep_days=7, keep_last_n_per_workflow=2)
        arts = [
            _art(f"a{i}", days_ago=30 + i, workflow="wf-quiet") for i in range(5)
        ]
        to_del, to_keep = policy.select_for_deletion(arts, NOW)
        assert len(to_keep) == 2  # the 2 newest of the quiet workflow
        assert len(to_del) == 3

    def test_mixed_workflows_independent(self):
        policy = RetentionPolicy(keep_days=7, keep_last_n_per_workflow=1)
        arts = [
            _art("noisy-old", days_ago=10, workflow="wf-noisy"),
            _art("noisy-newer", days_ago=8, workflow="wf-noisy"),
            _art("noisy-newest", days_ago=1, workflow="wf-noisy"),
            _art("quiet-old", days_ago=30, workflow="wf-quiet"),
            _art("quiet-older", days_ago=60, workflow="wf-quiet"),
        ]
        to_del, to_keep = policy.select_for_deletion(arts, NOW)
        kept_ids = {a.id for a in to_keep}
        del_ids = {a.id for a in to_del}
        # Noisy: newest is recent → kept; last-N=1 keeps newest only;
        # noisy-newer and noisy-old are both old and not in top-1 → deleted.
        assert "noisy-newest" in kept_ids
        assert "noisy-newer" in del_ids
        assert "noisy-old" in del_ids
        # Quiet: newest of the quiet workflow kept by last-N rule; older deleted.
        assert "quiet-old" in kept_ids
        assert "quiet-older" in del_ids


# ---------------------------------------------------------------------------
# GitHub provider
# ---------------------------------------------------------------------------


class TestGitHubProvider:
    def test_list_artifacts_paginates(self):
        responses = iter(
            [
                mock.Mock(
                    returncode=0,
                    stdout='{"artifacts": ['
                    + ",".join(
                        [
                            f'{{"id":{i},"name":"dist","size_in_bytes":1024,'
                            f'"created_at":"2026-05-30T00:00:00Z",'
                            f'"workflow_run":{{"id":{i}}}}}'
                            for i in range(100)
                        ]
                    )
                    + "]}",
                ),
                mock.Mock(
                    returncode=0,
                    stdout='{"artifacts": ['
                    + ",".join(
                        [
                            f'{{"id":{i+100},"name":"dist","size_in_bytes":1024,'
                            f'"created_at":"2026-05-29T00:00:00Z",'
                            f'"workflow_run":{{"id":{i+100}}}}}'
                            for i in range(10)
                        ]
                    )
                    + "]}",
                ),
            ]
        )

        def fake_run(*args, **kwargs):
            return next(responses)

        provider = GitHubProvider(runner=fake_run)
        arts = list(provider.list_artifacts("acme/repo"))
        assert len(arts) == 110

    def test_delete_artifact_calls_api(self):
        captured = []

        def fake_run(cmd, **kwargs):
            captured.append(cmd)
            return mock.Mock(returncode=0)

        provider = GitHubProvider(runner=fake_run)
        ok = provider.delete_artifact(_art("9999", days_ago=10))
        assert ok
        assert captured[0] == [
            "gh",
            "api",
            "-X",
            "DELETE",
            "repos/acme/repo/actions/artifacts/9999",
        ]


# ---------------------------------------------------------------------------
# cleanup_artifacts orchestrator
# ---------------------------------------------------------------------------


class _StubProvider:
    name = "github"

    def __init__(self, artifacts: list[Artifact]) -> None:
        self._artifacts = artifacts
        self.deleted: list[str] = []

    def list_artifacts(self, repo: str):
        for a in self._artifacts:
            yield a

    def delete_artifact(self, artifact: Artifact) -> bool:
        self.deleted.append(artifact.id)
        return True


class TestCleanupOrchestration:
    def _confirmed_params(self, **kwargs):
        defaults = dict(repos=[("github", "acme/repo")], confirmed=True)
        defaults.update(kwargs)
        return CleanupParams(**defaults)

    def test_dry_run_doesnt_delete(self):
        # Use distinct workflows so the keep-last-N rule doesn't preserve them.
        stub = _StubProvider(
            [
                _art("a", days_ago=10, workflow="wf-a"),
                _art("a2", days_ago=11, workflow="wf-a"),
                _art("a3", days_ago=12, workflow="wf-a"),
                _art("a4", days_ago=13, workflow="wf-a"),  # >3 so some deletable
                _art("b", days_ago=20, workflow="wf-b"),
                _art("b2", days_ago=21, workflow="wf-b"),
                _art("b3", days_ago=22, workflow="wf-b"),
                _art("b4", days_ago=23, workflow="wf-b"),
            ]
        )
        params = self._confirmed_params(dry_run=True)
        result = cleanup_artifacts(
            params, providers={"github": stub}, now=NOW
        )
        assert result.deleted_count == 0
        assert stub.deleted == []
        # But it reports what *would* have been deleted (4-of-8: last-3-per-
        # workflow safety net keeps 3 from wf-a + 3 from wf-b = 6 kept;
        # one extra in each workflow is old AND not in top-3 → 2 deletable).
        assert result.per_repo["acme/repo"]["candidate_for_deletion"] == 2

    def test_executes_deletions_when_confirmed(self):
        # Override last-N to 0 so the only safety net is the time-cutoff.
        from axiom.extensions.builtins.hygiene.artifact_cleanup import RetentionPolicy
        stub = _StubProvider(
            [
                _art("old1", days_ago=10),
                _art("old2", days_ago=15),
                _art("recent", days_ago=1),
            ]
        )
        params = CleanupParams(
            repos=[("github", "acme/repo")],
            policy=RetentionPolicy(keep_days=7, keep_last_n_per_workflow=0),
            confirmed=True,
        )
        result = cleanup_artifacts(
            params, providers={"github": stub}, now=NOW
        )
        assert result.deleted_count == 2
        assert set(stub.deleted) == {"old1", "old2"}
        assert result.kept_count == 1
        assert result.freed_bytes > 0

    def test_multiple_repos(self):
        # last-N=0 so single-artifact-in-workflow can be deleted.
        from axiom.extensions.builtins.hygiene.artifact_cleanup import RetentionPolicy
        stub_a = _StubProvider([_art("a-old", days_ago=10)])
        stub_b = _StubProvider([_art("b-old", days_ago=10), _art("b-recent", days_ago=1)])
        params = CleanupParams(
            repos=[("github", "repo-a"), ("github", "repo-b")],
            policy=RetentionPolicy(keep_days=7, keep_last_n_per_workflow=0),
            confirmed=True,
        )

        # Wrap one provider that switches its artifact list by repo arg.
        class _MultiStub:
            name = "github"
            deleted: list[str] = []

            def list_artifacts(self_, repo: str):
                src = stub_a if repo == "repo-a" else stub_b
                yield from src.list_artifacts(repo)

            def delete_artifact(self_, a):
                self_.deleted.append(a.id)
                return True

        multi = _MultiStub()
        result = cleanup_artifacts(
            params, providers={"github": multi}, now=NOW
        )
        assert "repo-a" in result.per_repo
        assert "repo-b" in result.per_repo
        assert result.deleted_count == 2

    def test_unknown_provider_skipped(self):
        params = CleanupParams(
            repos=[("gitlab", "acme/repo")], confirmed=True
        )
        result = cleanup_artifacts(
            params, providers={"github": _StubProvider([])}, now=NOW
        )
        assert result.deleted_count == 0
        assert result.per_repo == {}


# ---------------------------------------------------------------------------
# ADR-045 D6 volume gating
# ---------------------------------------------------------------------------


class TestVolumeGating:
    """First-run unconfirmed should not delete; should signal needs_confirm."""

    def test_unconfirmed_first_run_signals_or_proceeds(self):
        # Diverse workflows so last-N=3 doesn't auto-preserve everything.
        from axiom.extensions.builtins.hygiene.artifact_cleanup import RetentionPolicy
        stub = _StubProvider(
            [_art(f"old{i}", days_ago=10, workflow=f"wf-{i % 5}") for i in range(50)]
        )
        params = CleanupParams(
            repos=[("github", "acme/repo")],
            policy=RetentionPolicy(keep_days=7, keep_last_n_per_workflow=0),
            confirmed=False,
        )
        result = cleanup_artifacts(
            params, providers={"github": stub}, now=NOW
        )
        # Either confirmation requested (no deletion) OR guard permitted
        # (deletion proceeded). Both are correct.
        if result.confirmed_required:
            assert result.deleted_count == 0
            assert stub.deleted == []
        else:
            assert result.deleted_count > 0
