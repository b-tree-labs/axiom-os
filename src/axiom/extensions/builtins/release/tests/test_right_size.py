# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tests for ``right_size_pr`` — provider-agnostic recommendation engine."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from unittest import mock


from axiom.extensions.builtins.release.right_size.core import (
    ProposedChange,
    RightSizeContext,
    recommend,
)
from axiom.extensions.builtins.release.right_size.providers import (
    GitHubProvider,
    GitLabProvider,
    InFlightPR,
    PRDiff,
    detect_provider,
)


NOW = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Stub provider for deterministic recommendation tests
# ---------------------------------------------------------------------------


class StubProvider:
    name = "stub"

    def __init__(
        self,
        prs: list[InFlightPR] | None = None,
        diffs: dict[int, PRDiff] | None = None,
    ) -> None:
        self._prs = prs or []
        self._diffs = diffs or {}

    def list_in_flight_prs(self, repo, author=None):
        return list(self._prs)

    def diff_for_pr(self, repo, pr_number):
        return self._diffs.get(pr_number)


def _pr(
    number: int,
    *,
    title: str = "PR",
    is_draft: bool = False,
    days_ago: int = 0,
) -> InFlightPR:
    when = NOW - timedelta(days=days_ago)
    return InFlightPR(
        number=number,
        title=title,
        head_branch=f"feat/branch-{number}",
        base_branch="main",
        author="@me",
        url=f"https://github.com/acme/repo/pull/{number}",
        is_draft=is_draft,
        created_at=when,
        updated_at=when,
        provider="stub",
    )


def _diff(number: int, files: tuple[str, ...]) -> PRDiff:
    return PRDiff(
        pr_number=number,
        files=files,
        additions=50,
        deletions=10,
        provider="stub",
    )


def _change(
    files: tuple[str, ...] = ("src/a.py",),
    additions: int = 10,
    deletions: int = 0,
    intent: str = "test",
) -> ProposedChange:
    return ProposedChange(
        branch_name="feat/proposed",
        files=files,
        intent=intent,
        additions=additions,
        deletions=deletions,
    )


# ---------------------------------------------------------------------------
# recommend() — the four decision rules
# ---------------------------------------------------------------------------


class TestRecommendRules:
    def test_no_in_flight_prs_open_new(self):
        ctx = RightSizeContext(provider=StubProvider(), repo="acme/repo", now=NOW)
        rec = recommend(_change(), ctx)
        assert rec.kind == "open_new"

    def test_no_overlap_open_new(self):
        prs = [_pr(101)]
        diffs = {101: _diff(101, ("docs/x.md",))}
        ctx = RightSizeContext(
            provider=StubProvider(prs, diffs),
            repo="acme/repo",
            now=NOW,
        )
        rec = recommend(_change(files=("src/a.py",)), ctx)
        assert rec.kind == "open_new"

    def test_tiny_change_overlapping_nondraft_pr_folds(self):
        prs = [_pr(101, is_draft=False)]
        diffs = {101: _diff(101, ("src/a.py", "src/b.py"))}
        ctx = RightSizeContext(
            provider=StubProvider(prs, diffs),
            repo="acme/repo",
            now=NOW,
        )
        # 1 file, 5 lines → tiny.
        rec = recommend(_change(files=("src/a.py",), additions=5), ctx)
        assert rec.kind == "fold_into"
        assert rec.target_pr == 101
        assert 101 in rec.overlap

    def test_larger_change_with_overlap_stacks(self):
        prs = [_pr(101, is_draft=False)]
        diffs = {101: _diff(101, ("src/a.py", "src/b.py", "src/c.py"))}
        ctx = RightSizeContext(
            provider=StubProvider(prs, diffs),
            repo="acme/repo",
            now=NOW,
        )
        # 4 files, 300 lines → medium (above tiny), but overlap is partial (1/4).
        rec = recommend(
            _change(
                files=("src/a.py", "src/d.py", "src/e.py", "src/f.py"),
                additions=300,
            ),
            ctx,
        )
        assert rec.kind == "stack_on"
        assert rec.target_pr == 101

    def test_heavy_overlap_nondraft_recent_waits(self):
        prs = [_pr(101, is_draft=False)]
        diffs = {
            101: _diff(101, ("src/a.py", "src/b.py", "src/c.py", "src/d.py")),
        }
        ctx = RightSizeContext(
            provider=StubProvider(prs, diffs),
            repo="acme/repo",
            now=NOW,
        )
        # 4 files in the proposed change, all overlap → heavy.
        rec = recommend(
            _change(files=("src/a.py", "src/b.py", "src/c.py", "src/d.py")),
            ctx,
        )
        assert rec.kind == "wait_for"
        assert rec.target_pr == 101

    def test_draft_target_does_not_block_fold(self):
        # Even though the target is draft, fold is still safe because
        # the rule explicitly skips drafts (drafts aren't ready to merge).
        prs = [_pr(101, is_draft=True)]
        diffs = {101: _diff(101, ("src/a.py",))}
        ctx = RightSizeContext(
            provider=StubProvider(prs, diffs),
            repo="acme/repo",
            now=NOW,
        )
        rec = recommend(_change(files=("src/a.py",), additions=5), ctx)
        # Draft → falls through fold + wait rules → stacks.
        assert rec.kind == "stack_on"

    def test_stale_pr_does_not_force_wait(self):
        prs = [_pr(101, is_draft=False, days_ago=30)]
        diffs = {
            101: _diff(101, ("src/a.py", "src/b.py", "src/c.py", "src/d.py"))
        }
        ctx = RightSizeContext(
            provider=StubProvider(prs, diffs),
            repo="acme/repo",
            now=NOW,
        )
        rec = recommend(
            _change(files=("src/a.py", "src/b.py", "src/c.py", "src/d.py")),
            ctx,
        )
        # Stale target → fall through to stack (don't wait indefinitely).
        assert rec.kind == "stack_on"


class TestRecommendationRationale:
    def test_open_new_rationale_human_readable(self):
        ctx = RightSizeContext(provider=StubProvider(), repo="r", now=NOW)
        rec = recommend(_change(), ctx)
        assert "no overlap" in rec.rationale.lower()

    def test_fold_rationale_names_target_pr(self):
        prs = [_pr(101)]
        diffs = {101: _diff(101, ("src/a.py",))}
        ctx = RightSizeContext(
            provider=StubProvider(prs, diffs), repo="r", now=NOW
        )
        rec = recommend(_change(files=("src/a.py",), additions=5), ctx)
        assert "PR #101" in rec.rationale or "101" in rec.rationale


# ---------------------------------------------------------------------------
# Provider abstraction (Factory + Protocol)
# ---------------------------------------------------------------------------


class TestProviderFactory:
    def test_explicit_github(self):
        p = detect_provider(explicit="github")
        assert isinstance(p, GitHubProvider)

    def test_explicit_gitlab(self):
        p = detect_provider(explicit="gitlab")
        assert isinstance(p, GitLabProvider)

    def test_remote_url_github_match(self):
        p = detect_provider(remote_url="https://github.com/acme/repo.git")
        assert isinstance(p, GitHubProvider)

    def test_remote_url_gitlab_match(self):
        p = detect_provider(remote_url="https://gitlab.com/acme/repo.git")
        assert isinstance(p, GitLabProvider)

    def test_remote_url_self_hosted_gitlab(self):
        p = detect_provider(remote_url="https://gitlab.example.com/acme/repo")
        assert isinstance(p, GitLabProvider)

    def test_default_is_github(self):
        p = detect_provider()
        assert isinstance(p, GitHubProvider)


# ---------------------------------------------------------------------------
# GitHub adapter — parses `gh` output
# ---------------------------------------------------------------------------


class TestGitHubAdapter:
    def test_list_in_flight_parses_gh_json(self):
        payload = json.dumps([
            {
                "number": 101,
                "title": "PR title",
                "headRefName": "feat/x",
                "baseRefName": "main",
                "author": {"login": "@me"},
                "url": "https://github.com/acme/repo/pull/101",
                "isDraft": False,
                "createdAt": "2026-06-01T11:00:00Z",
                "updatedAt": "2026-06-01T11:30:00Z",
            }
        ])

        def fake_runner(*args, **kwargs):
            return mock.Mock(returncode=0, stdout=payload)

        provider = GitHubProvider(runner=fake_runner)
        prs = provider.list_in_flight_prs("acme/repo")
        assert len(prs) == 1
        assert prs[0].number == 101
        assert prs[0].head_branch == "feat/x"
        assert prs[0].is_draft is False

    def test_diff_for_pr_parses_gh_json(self):
        payload = json.dumps(
            {
                "files": [{"path": "src/a.py"}, {"path": "src/b.py"}],
                "additions": 50,
                "deletions": 10,
            }
        )

        def fake_runner(*args, **kwargs):
            return mock.Mock(returncode=0, stdout=payload)

        provider = GitHubProvider(runner=fake_runner)
        diff = provider.diff_for_pr("acme/repo", 101)
        assert diff is not None
        assert diff.files == ("src/a.py", "src/b.py")
        assert diff.additions == 50

    def test_gh_missing_returns_empty(self):
        def fake_runner(*args, **kwargs):
            raise FileNotFoundError("no gh")

        provider = GitHubProvider(runner=fake_runner)
        assert provider.list_in_flight_prs("acme/repo") == []
        assert provider.diff_for_pr("acme/repo", 1) is None


# ---------------------------------------------------------------------------
# Skill entrypoint (ADR-056 contract)
# ---------------------------------------------------------------------------


class TestSkillContract:
    def test_skill_returns_recommendation(self, monkeypatch):
        # Stub the provider factory so the skill doesn't shell out.
        from axiom.extensions.builtins.release.right_size import skill as skill_mod

        monkeypatch.setattr(
            skill_mod, "detect_provider", lambda **kw: StubProvider()
        )
        result = skill_mod.right_size_pr(
            {
                "repo": "acme/repo",
                "branch_name": "feat/proposed",
                "files": ["src/a.py"],
                "intent": "small bug fix",
            },
            ctx=None,  # type: ignore[arg-type]
        )
        assert result.ok is True
        assert result.value["kind"] == "open_new"

    def test_missing_param_returns_error(self):
        from axiom.extensions.builtins.release.right_size import skill as skill_mod

        result = skill_mod.right_size_pr({"repo": "acme/repo"}, ctx=None)  # type: ignore[arg-type]
        assert result.ok is False
        assert any("missing" in e.lower() for e in result.errors)


# ---------------------------------------------------------------------------
# Self-incident regression — the 7-sequential-PR pattern of 2026-05-31
# ---------------------------------------------------------------------------


class TestSequentialPRWastePattern:
    """The exact 2026-05-31 pattern: an agent opens consecutive PRs touching
    governance/hygiene. The 2nd-onwards should be folded or stacked.
    """

    def test_second_governance_pr_folds_into_first(self):
        # First PR: governance foundation types.
        pr_101 = _pr(101, title="feat(governance): foundation types")
        diff_101 = PRDiff(
            pr_number=101,
            files=("src/axiom/governance/envelope.py",
                   "src/axiom/governance/capability.py",
                   "src/axiom/governance/verdict.py"),
            additions=600,
            deletions=0,
            provider="stub",
        )
        # Second PR proposed: tiny tweak to envelope only.
        change = ProposedChange(
            branch_name="fix/envelope-typo",
            files=("src/axiom/governance/envelope.py",),
            intent="fix typo in envelope docstring",
            additions=2,
            deletions=2,
        )
        ctx = RightSizeContext(
            provider=StubProvider([pr_101], {101: diff_101}),
            repo="b-tree-labs/axiom-os",
            now=NOW,
        )
        rec = recommend(change, ctx)
        assert rec.kind == "fold_into"
        assert rec.target_pr == 101
        # CI minutes that would have been wasted on a standalone PR.
        assert rec.cost_estimate_minutes > 0
