# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tests for `pr_check_auto_closer` — Layer 3b of the RIVET watcher.

On a recovery flip (failing → passing), find any open issues opened
by the `github-actions` bot matching the title pattern
``🔴 CI failed on `refs/pull/<N>/merge` (<sha>)`` for that PR, and
close each one with a comment referencing the recovery.

This is the first **destructive** op in the watcher pipeline, so the
defenses are deliberate:

  - Strict title regex anchored on the PR's merge-ref
  - Author must be `app/github-actions` (or `github-actions[bot]`)
  - State must be `open`
  - Each close is logged with the matched evidence
  - Dry-run mode via `RIVET_AUTO_CLOSE_DRY_RUN=1` env var
  - Hard disable via `RIVET_AUTO_CLOSE=0`
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field


# ---------------------------------------------------------------------------
# Test double for the gh seam
# ---------------------------------------------------------------------------


@dataclass
class CapturedGhCall:
    args: list[str]


@dataclass
class FakeGh:
    calls: list[CapturedGhCall] = field(default_factory=list)
    replies: list[str] = field(default_factory=list)

    def __call__(self, args: list[str]) -> str:
        self.calls.append(CapturedGhCall(args=list(args)))
        if self.replies:
            return self.replies.pop(0)
        return ""


def _patch_gh(monkeypatch, fake: FakeGh):
    from axiom.extensions.builtins.release import pr_check_auto_closer
    monkeypatch.setattr(pr_check_auto_closer, "_run_gh", fake)


def _stale_issue_json(*, number, pr_number, sha="abc123"):
    return {
        "number": number,
        "title": f"🔴 CI failed on `refs/pull/{pr_number}/merge` ({sha})",
        "author": {"login": "app/github-actions", "is_bot": True},
        "state": "OPEN",
    }


# ---------------------------------------------------------------------------
# find_stale_pr_issues
# ---------------------------------------------------------------------------


class TestFindStalePrIssues:
    def test_returns_issues_matching_pr_merge_ref(self, monkeypatch):
        from axiom.extensions.builtins.release.pr_check_auto_closer import (
            find_stale_pr_issues,
        )
        fake = FakeGh(replies=[json.dumps([
            _stale_issue_json(number=199, pr_number=197),
            _stale_issue_json(number=198, pr_number=197, sha="def456"),
            _stale_issue_json(number=194, pr_number=999),  # different PR
            {"number": 100, "title": "feature: unrelated",
             "author": {"login": "ben", "is_bot": False}, "state": "OPEN"},
        ])])
        _patch_gh(monkeypatch, fake)

        results = find_stale_pr_issues(pr_number=197)
        nums = [r.number for r in results]
        assert nums == [199, 198]

    def test_filters_out_non_bot_authors(self, monkeypatch):
        """Even with a matching title, refuse to close an issue opened
        by a human — they may have copied the title for a reason."""
        from axiom.extensions.builtins.release.pr_check_auto_closer import (
            find_stale_pr_issues,
        )
        fake = FakeGh(replies=[json.dumps([
            {"number": 300,
             "title": "🔴 CI failed on `refs/pull/197/merge` (abc)",
             "author": {"login": "ben", "is_bot": False},
             "state": "OPEN"},
        ])])
        _patch_gh(monkeypatch, fake)

        assert find_stale_pr_issues(pr_number=197) == []

    def test_filters_out_closed_issues(self, monkeypatch):
        from axiom.extensions.builtins.release.pr_check_auto_closer import (
            find_stale_pr_issues,
        )
        closed = _stale_issue_json(number=199, pr_number=197)
        closed["state"] = "CLOSED"
        fake = FakeGh(replies=[json.dumps([closed])])
        _patch_gh(monkeypatch, fake)
        assert find_stale_pr_issues(pr_number=197) == []

    def test_returns_empty_on_gh_failure(self, monkeypatch):
        """gh CLI failure → no close attempts. Better to leave stale
        issues than guess at what to close from empty input."""
        from axiom.extensions.builtins.release.pr_check_auto_closer import (
            find_stale_pr_issues,
        )
        fake = FakeGh(replies=[""])
        _patch_gh(monkeypatch, fake)
        assert find_stale_pr_issues(pr_number=197) == []


# ---------------------------------------------------------------------------
# close_stale_issue
# ---------------------------------------------------------------------------


class TestCloseStaleIssue:
    def test_close_invokes_gh_issue_close_with_comment(self, monkeypatch):
        """Close path: 1) fetch title (for emoji swap), 2) close with
        comment, 3) best-effort title edit. The close call itself
        carries the operator-provided comment."""
        from axiom.extensions.builtins.release.pr_check_auto_closer import (
            close_stale_issue,
        )
        fake = FakeGh(replies=["", "", ""])  # view, close, edit
        _patch_gh(monkeypatch, fake)

        ok = close_stale_issue(issue_number=199, comment="recovered in def456")
        assert ok is True
        # The close call is in the sequence with the comment
        close_call = next(
            c for c in fake.calls if c.args[:2] == ["issue", "close"]
        )
        assert close_call.args[:3] == ["issue", "close", "199"]
        assert "--comment" in close_call.args
        joined = " ".join(close_call.args)
        assert "recovered in def456" in joined


# ---------------------------------------------------------------------------
# auto_close_on_recovery (the integration entry)
# ---------------------------------------------------------------------------


def _make_recovery_flip(pr_number=197, head_sha="abc123"):
    from axiom.extensions.builtins.release.pr_check_watcher import StateFlip
    return StateFlip(
        pr_number=pr_number, title="feat: ...",
        url=f"https://github.com/o/r/pull/{pr_number}",
        head_branch="feat/x",
        from_state="failing", to_state="passing",
        failing_rows=[], classification="unknown",
    )


class TestAutoCloseOnRecovery:
    def test_recovery_flip_closes_matching_stale_issues(self, monkeypatch):
        from axiom.extensions.builtins.release.pr_check_auto_closer import (
            auto_close_on_recovery,
        )
        # First gh call: list stale issues; subsequent calls: close each.
        fake = FakeGh(replies=[
            json.dumps([
                _stale_issue_json(number=199, pr_number=197),
                _stale_issue_json(number=198, pr_number=197, sha="def456"),
            ]),
            "",  # close 199
            "",  # close 198
        ])
        _patch_gh(monkeypatch, fake)

        closed = auto_close_on_recovery(_make_recovery_flip(pr_number=197))
        assert sorted(c.number for c in closed) == [198, 199]
        # Verify each close was actually invoked
        close_calls = [c for c in fake.calls if c.args[:2] == ["issue", "close"]]
        assert len(close_calls) == 2

    def test_non_recovery_flip_does_nothing(self, monkeypatch):
        from axiom.extensions.builtins.release.pr_check_auto_closer import (
            auto_close_on_recovery,
        )
        from axiom.extensions.builtins.release.pr_check_watcher import StateFlip
        fake = FakeGh()
        _patch_gh(monkeypatch, fake)

        # Failing flip, not recovery
        failing = StateFlip(
            pr_number=197, title="", url="", head_branch="",
            from_state="passing", to_state="failing",
            failing_rows=[], classification="code",
        )
        assert auto_close_on_recovery(failing) == []
        assert fake.calls == []

    def test_dry_run_lists_but_does_not_close(self, monkeypatch):
        from axiom.extensions.builtins.release.pr_check_auto_closer import (
            auto_close_on_recovery,
        )
        monkeypatch.setenv("RIVET_AUTO_CLOSE_DRY_RUN", "1")
        fake = FakeGh(replies=[json.dumps([
            _stale_issue_json(number=199, pr_number=197),
        ])])
        _patch_gh(monkeypatch, fake)

        closed = auto_close_on_recovery(_make_recovery_flip(pr_number=197))
        # Issue is "would-close" — returned for surface, but no close call
        assert [c.number for c in closed] == [199]
        close_calls = [c for c in fake.calls if c.args[:2] == ["issue", "close"]]
        assert close_calls == []

    def test_hard_disable_short_circuits_entirely(self, monkeypatch):
        """RIVET_AUTO_CLOSE=0 prevents even the list call — useful when
        the operator wants RIVET silent on issue management."""
        from axiom.extensions.builtins.release.pr_check_auto_closer import (
            auto_close_on_recovery,
        )
        monkeypatch.setenv("RIVET_AUTO_CLOSE", "0")
        fake = FakeGh()
        _patch_gh(monkeypatch, fake)

        assert auto_close_on_recovery(_make_recovery_flip(pr_number=197)) == []
        assert fake.calls == []

    def test_close_comment_references_recovery(self, monkeypatch):
        from axiom.extensions.builtins.release.pr_check_auto_closer import (
            auto_close_on_recovery,
        )
        fake = FakeGh(replies=[
            json.dumps([_stale_issue_json(number=199, pr_number=197)]),
            "",
        ])
        _patch_gh(monkeypatch, fake)

        auto_close_on_recovery(_make_recovery_flip(pr_number=197))
        close_call = [c for c in fake.calls if c.args[:2] == ["issue", "close"]][0]
        joined = " ".join(close_call.args)
        assert "recovered" in joined.lower()
        # References the PR so the comment is traceable
        assert "197" in joined or "#197" in joined


# ---------------------------------------------------------------------------
# Manual sweep — slice 21
# ---------------------------------------------------------------------------


def _main_issue_json(*, number, sha="abc123"):
    return {
        "number": number,
        "title": f"🔴 CI failed on `main` ({sha})",
        "author": {"login": "app/github-actions", "is_bot": True},
        "state": "OPEN",
    }


class TestFindStaleMainIssues:
    def test_returns_only_main_branch_issues(self, monkeypatch):
        from axiom.extensions.builtins.release.pr_check_auto_closer import (
            find_stale_main_issues,
        )
        fake = FakeGh(replies=[json.dumps([
            _main_issue_json(number=194),
            _main_issue_json(number=186, sha="def"),
            _stale_issue_json(number=199, pr_number=197),  # PR ref, not main
            {"number": 100, "title": "unrelated", "state": "OPEN",
             "author": {"login": "ben", "is_bot": False}},
        ])])
        _patch_gh(monkeypatch, fake)

        results = find_stale_main_issues()
        assert sorted(r.number for r in results) == [186, 194]


class TestCurrentState:
    def test_current_pr_state_passing(self, monkeypatch):
        from axiom.extensions.builtins.release.pr_check_auto_closer import (
            current_pr_state,
        )
        fake = FakeGh(replies=[json.dumps([
            {"name": "Lint", "bucket": "pass", "state": "success",
             "description": "", "link": ""},
        ])])
        _patch_gh(monkeypatch, fake)
        assert current_pr_state(pr_number=211) == "passing"

    def test_current_pr_state_failing(self, monkeypatch):
        from axiom.extensions.builtins.release.pr_check_auto_closer import (
            current_pr_state,
        )
        fake = FakeGh(replies=[json.dumps([
            {"name": "Build", "bucket": "fail", "state": "failure",
             "description": "boom", "link": ""},
        ])])
        _patch_gh(monkeypatch, fake)
        assert current_pr_state(pr_number=211) == "failing"

    def test_current_main_state_passing(self, monkeypatch):
        from axiom.extensions.builtins.release.pr_check_auto_closer import (
            current_main_state,
        )
        fake = FakeGh(replies=[json.dumps([
            {"headBranch": "main", "status": "completed",
             "conclusion": "success", "url": "..."},
        ])])
        _patch_gh(monkeypatch, fake)
        assert current_main_state() == "passing"

    def test_current_main_state_failing(self, monkeypatch):
        from axiom.extensions.builtins.release.pr_check_auto_closer import (
            current_main_state,
        )
        fake = FakeGh(replies=[json.dumps([
            {"headBranch": "main", "status": "completed",
             "conclusion": "failure", "url": "..."},
        ])])
        _patch_gh(monkeypatch, fake)
        assert current_main_state() == "failing"

    def test_current_main_state_in_progress(self, monkeypatch):
        from axiom.extensions.builtins.release.pr_check_auto_closer import (
            current_main_state,
        )
        fake = FakeGh(replies=[json.dumps([
            {"headBranch": "main", "status": "in_progress",
             "conclusion": "", "url": "..."},
        ])])
        _patch_gh(monkeypatch, fake)
        assert current_main_state() == "pending"


class TestSweepStale:
    def test_pr_sweep_closes_only_if_pr_currently_passing(self, monkeypatch):
        from axiom.extensions.builtins.release.pr_check_auto_closer import (
            sweep_stale,
        )
        fake = FakeGh(replies=[
            # current_pr_state(197) → passing
            json.dumps([{"name": "Lint", "bucket": "pass", "state": "success",
                         "description": "", "link": ""}]),
            # find_stale_pr_issues(197) → 1 match
            json.dumps([_stale_issue_json(number=199, pr_number=197)]),
            # close 199
            "",
        ])
        _patch_gh(monkeypatch, fake)

        report = sweep_stale(pr_number=197)
        assert [c.number for c in report.closed] == [199]
        # Verify we actually issued a close call (not just listed)
        close_calls = [c for c in fake.calls if c.args[:2] == ["issue", "close"]]
        assert len(close_calls) == 1

    def test_pr_sweep_refuses_when_pr_still_failing(self, monkeypatch):
        """Don't close issues for a PR whose CI is currently failing.
        Closing while red would lose the operator's signal that
        something's still broken."""
        from axiom.extensions.builtins.release.pr_check_auto_closer import (
            sweep_stale,
        )
        fake = FakeGh(replies=[
            # current_pr_state(197) → failing
            json.dumps([{"name": "Build", "bucket": "fail", "state": "failure",
                         "description": "boom", "link": ""}]),
        ])
        _patch_gh(monkeypatch, fake)

        report = sweep_stale(pr_number=197)
        assert report.closed == []
        assert report.skipped_reason == "pr_not_passing"
        close_calls = [c for c in fake.calls if c.args[:2] == ["issue", "close"]]
        assert close_calls == []

    def test_main_sweep_closes_only_if_main_currently_passing(
        self, monkeypatch,
    ):
        from axiom.extensions.builtins.release.pr_check_auto_closer import (
            sweep_stale,
        )
        fake = FakeGh(replies=[
            # current_main_state → passing
            json.dumps([{"headBranch": "main", "status": "completed",
                         "conclusion": "success", "url": ""}]),
            # find_stale_main_issues → 2
            json.dumps([
                _main_issue_json(number=194),
                _main_issue_json(number=186, sha="def"),
            ]),
            "", "",  # close 194, close 186
        ])
        _patch_gh(monkeypatch, fake)

        report = sweep_stale(all_main=True)
        assert sorted(c.number for c in report.closed) == [186, 194]

    def test_main_sweep_refuses_when_main_failing(self, monkeypatch):
        from axiom.extensions.builtins.release.pr_check_auto_closer import (
            sweep_stale,
        )
        fake = FakeGh(replies=[
            json.dumps([{"headBranch": "main", "status": "completed",
                         "conclusion": "failure", "url": ""}]),
        ])
        _patch_gh(monkeypatch, fake)

        report = sweep_stale(all_main=True)
        assert report.closed == []
        assert report.skipped_reason == "main_not_passing"

    def test_sweep_dry_run_lists_without_closing(self, monkeypatch):
        from axiom.extensions.builtins.release.pr_check_auto_closer import (
            sweep_stale,
        )
        fake = FakeGh(replies=[
            json.dumps([{"name": "Lint", "bucket": "pass",
                         "state": "success", "description": "", "link": ""}]),
            json.dumps([_stale_issue_json(number=199, pr_number=197)]),
        ])
        _patch_gh(monkeypatch, fake)

        report = sweep_stale(pr_number=197, dry_run=True)
        assert [c.number for c in report.closed] == [199]
        close_calls = [c for c in fake.calls if c.args[:2] == ["issue", "close"]]
        assert close_calls == []


# ---------------------------------------------------------------------------
# --all-prs sweep (slice 22)
# ---------------------------------------------------------------------------


def _pr_view_json(*, number, state, merged_at=None):
    return {
        "number": number, "state": state,
        "mergedAt": merged_at,
    }


class TestPrSafetyClassification:
    """`_pr_safe_to_close_stale(N)` decides whether stale 🔴 issues
    for PR #N can be safely closed:

      - OPEN + passing CI → safe
      - OPEN + failing CI → not safe (operator may still be debugging)
      - MERGED + main passing → safe (content is in main, main is green)
      - MERGED + main failing → not safe (main broke; the prior failure
        may resurface in the merged work)
      - CLOSED (not merged) → safe (work abandoned)
    """

    def test_open_passing_is_safe(self, monkeypatch):
        from axiom.extensions.builtins.release.pr_check_auto_closer import (
            _pr_safe_to_close_stale,
        )
        fake = FakeGh(replies=[
            # gh pr view 197 --json state,mergedAt
            json.dumps(_pr_view_json(number=197, state="OPEN")),
            # current_pr_state(197) → passing
            json.dumps([{"name": "Lint", "bucket": "pass", "state": "success",
                         "description": "", "link": ""}]),
        ])
        _patch_gh(monkeypatch, fake)
        safe, _reason = _pr_safe_to_close_stale(pr_number=197)
        assert safe is True

    def test_open_failing_is_not_safe(self, monkeypatch):
        from axiom.extensions.builtins.release.pr_check_auto_closer import (
            _pr_safe_to_close_stale,
        )
        fake = FakeGh(replies=[
            json.dumps(_pr_view_json(number=197, state="OPEN")),
            json.dumps([{"name": "Build", "bucket": "fail", "state": "failure",
                         "description": "boom", "link": ""}]),
        ])
        _patch_gh(monkeypatch, fake)
        safe, reason = _pr_safe_to_close_stale(pr_number=197)
        assert safe is False
        assert "still failing" in reason or "not passing" in reason

    def test_merged_with_main_passing_is_safe(self, monkeypatch):
        from axiom.extensions.builtins.release.pr_check_auto_closer import (
            _pr_safe_to_close_stale,
        )
        fake = FakeGh(replies=[
            json.dumps(_pr_view_json(
                number=197, state="MERGED",
                merged_at="2026-05-15T12:00:00Z")),
            # current_main_state → passing
            json.dumps([{"headBranch": "main", "status": "completed",
                         "conclusion": "success", "url": ""}]),
        ])
        _patch_gh(monkeypatch, fake)
        safe, _reason = _pr_safe_to_close_stale(pr_number=197)
        assert safe is True

    def test_merged_with_main_failing_is_not_safe(self, monkeypatch):
        from axiom.extensions.builtins.release.pr_check_auto_closer import (
            _pr_safe_to_close_stale,
        )
        fake = FakeGh(replies=[
            json.dumps(_pr_view_json(
                number=197, state="MERGED",
                merged_at="2026-05-15T12:00:00Z")),
            json.dumps([{"headBranch": "main", "status": "completed",
                         "conclusion": "failure", "url": ""}]),
        ])
        _patch_gh(monkeypatch, fake)
        safe, reason = _pr_safe_to_close_stale(pr_number=197)
        assert safe is False
        assert "main" in reason.lower()

    def test_closed_not_merged_is_safe_by_abandonment(self, monkeypatch):
        from axiom.extensions.builtins.release.pr_check_auto_closer import (
            _pr_safe_to_close_stale,
        )
        fake = FakeGh(replies=[
            # state=CLOSED with mergedAt=null → abandoned
            json.dumps(_pr_view_json(number=197, state="CLOSED")),
        ])
        _patch_gh(monkeypatch, fake)
        safe, _reason = _pr_safe_to_close_stale(pr_number=197)
        assert safe is True


class TestAllPrsSweep:
    def test_all_prs_sweep_closes_safe_and_skips_unsafe(self, monkeypatch):
        """Two PRs with stale issues. PR 197 is OPEN+passing → close.
        PR 200 is OPEN+failing → skip."""
        from axiom.extensions.builtins.release.pr_check_auto_closer import (
            sweep_stale,
        )
        fake = FakeGh(replies=[
            # find_all_pr_ref_issues → 3 issues across 2 PRs
            json.dumps([
                _stale_issue_json(number=199, pr_number=197),
                _stale_issue_json(number=198, pr_number=197, sha="def"),
                _stale_issue_json(number=205, pr_number=200),
            ]),
            # PR 197: gh pr view → OPEN
            json.dumps(_pr_view_json(number=197, state="OPEN")),
            # PR 197: current state → passing
            json.dumps([{"name": "Lint", "bucket": "pass", "state": "success",
                         "description": "", "link": ""}]),
            # close 199
            "",
            # close 198
            "",
            # PR 200: gh pr view → OPEN
            json.dumps(_pr_view_json(number=200, state="OPEN")),
            # PR 200: current state → failing (skip)
            json.dumps([{"name": "Build", "bucket": "fail", "state": "failure",
                         "description": "still red", "link": ""}]),
        ])
        _patch_gh(monkeypatch, fake)

        report = sweep_stale(all_prs=True)
        # Only PR 197's two issues closed; PR 200's skipped
        assert sorted(c.number for c in report.closed) == [198, 199]
        close_calls = [c for c in fake.calls if c.args[:2] == ["issue", "close"]]
        assert len(close_calls) == 2

    def test_all_prs_dry_run_lists_safe_without_closing(self, monkeypatch):
        from axiom.extensions.builtins.release.pr_check_auto_closer import (
            sweep_stale,
        )
        fake = FakeGh(replies=[
            json.dumps([_stale_issue_json(number=199, pr_number=197)]),
            json.dumps(_pr_view_json(
                number=197, state="MERGED",
                merged_at="2026-05-15T12:00:00Z")),
            json.dumps([{"headBranch": "main", "status": "completed",
                         "conclusion": "success", "url": ""}]),
        ])
        _patch_gh(monkeypatch, fake)

        report = sweep_stale(all_prs=True, dry_run=True)
        assert [c.number for c in report.closed] == [199]
        close_calls = [c for c in fake.calls if c.args[:2] == ["issue", "close"]]
        assert close_calls == []

    def test_all_prs_empty_when_no_stale_issues(self, monkeypatch):
        from axiom.extensions.builtins.release.pr_check_auto_closer import (
            sweep_stale,
        )
        fake = FakeGh(replies=[json.dumps([])])
        _patch_gh(monkeypatch, fake)
        report = sweep_stale(all_prs=True)
        assert report.closed == []
        assert report.skipped_reason == ""


# ---------------------------------------------------------------------------
# --all-tags sweep (slice 23)
# ---------------------------------------------------------------------------


def _tag_issue_json(*, number, tag, sha="abc123"):
    return {
        "number": number,
        "title": f"🔴 CI failed on `{tag}` ({sha})",
        "author": {"login": "app/github-actions", "is_bot": True},
        "state": "OPEN",
    }


def _patch_git(monkeypatch, returns: dict[tuple, int]):
    """Replace `_run_git` with a function that returns rc per argv tuple.

    `returns` maps tuple(argv) → exit code (0=success, non-zero=fail).
    Unknown argv → 1 (not reachable / unknown tag).
    """
    from axiom.extensions.builtins.release import pr_check_auto_closer

    def fake(args: list[str]) -> int:
        return returns.get(tuple(args), 1)

    monkeypatch.setattr(pr_check_auto_closer, "_run_git", fake)


class TestFindStaleTagIssues:
    def test_returns_tag_ref_issues_only(self, monkeypatch):
        from axiom.extensions.builtins.release.pr_check_auto_closer import (
            find_stale_tag_issues,
        )
        fake = FakeGh(replies=[json.dumps([
            _tag_issue_json(number=128, tag="v0.14.0"),
            _tag_issue_json(number=97, tag="v0.12.0", sha="def"),
            _stale_issue_json(number=199, pr_number=197),  # PR ref, not tag
            _main_issue_json(number=194),                   # main ref, not tag
        ])])
        _patch_gh(monkeypatch, fake)
        results = find_stale_tag_issues()
        assert sorted((r.number, r.matched_tag) for r in results) == [
            (97, "v0.12.0"), (128, "v0.14.0"),
        ]


class TestTagSafetyClassification:
    def test_tag_reachable_from_main_is_safe(self, monkeypatch):
        from axiom.extensions.builtins.release.pr_check_auto_closer import (
            _tag_safe_to_close_stale,
        )
        # git merge-base --is-ancestor refs/tags/v0.12.0 origin/main → 0
        _patch_git(monkeypatch, {
            ("merge-base", "--is-ancestor", "refs/tags/v0.12.0",
             "origin/main"): 0,
        })
        safe, _reason = _tag_safe_to_close_stale(tag="v0.12.0")
        assert safe is True

    def test_tag_not_reachable_is_not_safe(self, monkeypatch):
        from axiom.extensions.builtins.release.pr_check_auto_closer import (
            _tag_safe_to_close_stale,
        )
        # ancestry check fails (returns 1) → not safe
        _patch_git(monkeypatch, {})
        safe, reason = _tag_safe_to_close_stale(tag="v0.99.99-unmerged")
        assert safe is False
        assert "main" in reason.lower() or "reachable" in reason.lower()


class TestAllTagsSweep:
    def test_all_tags_sweep_closes_safe_only(self, monkeypatch):
        from axiom.extensions.builtins.release.pr_check_auto_closer import (
            sweep_stale,
        )
        fake = FakeGh(replies=[
            # find_stale_tag_issues → 3 across 2 tags
            json.dumps([
                _tag_issue_json(number=128, tag="v0.14.0"),
                _tag_issue_json(number=97, tag="v0.12.0"),
                _tag_issue_json(number=999, tag="v0.99.99-bad"),
            ]),
            "", "",  # close 128, close 97 (in tag-sorted order)
        ])
        _patch_gh(monkeypatch, fake)
        # v0.14.0 + v0.12.0 reachable, v0.99.99-bad NOT
        _patch_git(monkeypatch, {
            ("merge-base", "--is-ancestor", "refs/tags/v0.14.0",
             "origin/main"): 0,
            ("merge-base", "--is-ancestor", "refs/tags/v0.12.0",
             "origin/main"): 0,
        })

        report = sweep_stale(all_tags=True)
        assert sorted(c.number for c in report.closed) == [97, 128]
        close_calls = [c for c in fake.calls if c.args[:2] == ["issue", "close"]]
        assert len(close_calls) == 2

    def test_all_tags_dry_run(self, monkeypatch):
        from axiom.extensions.builtins.release.pr_check_auto_closer import (
            sweep_stale,
        )
        fake = FakeGh(replies=[
            json.dumps([_tag_issue_json(number=128, tag="v0.14.0")]),
        ])
        _patch_gh(monkeypatch, fake)
        _patch_git(monkeypatch, {
            ("merge-base", "--is-ancestor", "refs/tags/v0.14.0",
             "origin/main"): 0,
        })

        report = sweep_stale(all_tags=True, dry_run=True)
        assert [c.number for c in report.closed] == [128]
        close_calls = [c for c in fake.calls if c.args[:2] == ["issue", "close"]]
        assert close_calls == []
