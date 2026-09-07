# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tests for `pr_check_watcher` — RIVET's PR-scoped CI watcher skill.

The existing `ci_monitor._check_github` polls one latest run at the
top-level — it cannot see *PR-scoped* checks, which is exactly the
gap that let PR #211's `Build Wheel` billing-block failure fly under
RIVET's radar (2026-05-19 incident).

`pr_check_watcher` fills that gap:
  - enumerate the user's open PRs (`gh pr list --author @me`)
  - fetch per-job checks per PR (`gh pr checks <n> --json ...`)
  - classify failing checks: code | infra | flake | unknown
  - persist last-seen state under `~/.axi/agents/rivet/pr-checks.json`
  - emit `StateFlip` events on transitions for the heartbeat / AXI
    layer to surface

Tests stub `subprocess.run` so they don't touch the network. The flow
is mock-friendly: `_run_gh` is the seam.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# Static shapes
# ---------------------------------------------------------------------------


class TestCheckRow:
    def test_is_failing_only_for_fail_bucket(self):
        from axiom.extensions.builtins.release.pr_check_watcher import CheckRow

        passing = CheckRow(name="Lint", bucket="pass", state="success",
                           description="", link="")
        failing = CheckRow(name="Build Wheel", bucket="fail",
                           state="failure", description="boom", link="")
        skipping = CheckRow(name="Publish", bucket="skipping",
                            state="skipping", description="", link="")
        pending = CheckRow(name="Tests", bucket="pending",
                           state="in_progress", description="", link="")

        assert passing.is_failing is False
        assert failing.is_failing is True
        assert skipping.is_failing is False
        assert pending.is_failing is False


class TestPRChecksOverall:
    def test_all_pass_is_passing(self):
        from axiom.extensions.builtins.release.pr_check_watcher import (
            CheckRow, PRChecks,
        )
        rows = [
            CheckRow("Lint", "pass", "success", "", ""),
            CheckRow("Tests", "pass", "success", "", ""),
        ]
        pr = PRChecks(pr_number=1, title="x", url="u", head_branch="b", rows=rows)
        assert pr.overall == "passing"

    def test_any_fail_is_failing(self):
        from axiom.extensions.builtins.release.pr_check_watcher import (
            CheckRow, PRChecks,
        )
        rows = [
            CheckRow("Lint", "pass", "success", "", ""),
            CheckRow("Build", "fail", "failure", "boom", ""),
        ]
        pr = PRChecks(pr_number=1, title="x", url="u", head_branch="b", rows=rows)
        assert pr.overall == "failing"

    def test_pending_without_fail_is_pending(self):
        from axiom.extensions.builtins.release.pr_check_watcher import (
            CheckRow, PRChecks,
        )
        rows = [
            CheckRow("Lint", "pass", "success", "", ""),
            CheckRow("Tests", "pending", "in_progress", "", ""),
        ]
        pr = PRChecks(pr_number=1, title="x", url="u", head_branch="b", rows=rows)
        assert pr.overall == "pending"

    def test_skipping_only_is_passing(self):
        """Skipped checks (e.g. release jobs in PR mode) don't count as
        failures or pending — they just don't apply."""
        from axiom.extensions.builtins.release.pr_check_watcher import (
            CheckRow, PRChecks,
        )
        rows = [CheckRow("Publish", "skipping", "skipping", "", "")]
        pr = PRChecks(pr_number=1, title="x", url="u", head_branch="b", rows=rows)
        assert pr.overall == "passing"


# ---------------------------------------------------------------------------
# Failure classification
# ---------------------------------------------------------------------------


class TestClassifyFailure:
    """Distinguishing infra-failure (billing, runner unavailable, etc.)
    from code-failure (assertion, exit non-zero from the test step) is
    load-bearing — the responder routes them differently."""

    @pytest.mark.parametrize("text", [
        "The job was not started because recent account payments have failed",
        "spending limit needs to be increased",
        "Billing & plans",
        "GitHub Actions usage limit",
        "runner unavailable",
        "could not start runner",
        "queue limit exceeded",
    ])
    def test_billing_and_runner_phrases_are_infra(self, text):
        from axiom.extensions.builtins.release.pr_check_watcher import (
            CheckRow, classify_failure,
        )
        row = CheckRow(name="Build Wheel", bucket="fail",
                       state="failure", description=text, link="")
        assert classify_failure(row) == "infra"

    def test_default_is_code(self):
        """When no infra phrase matches, conservative default is 'code'
        — the responder treats unknowns as needing a code fix."""
        from axiom.extensions.builtins.release.pr_check_watcher import (
            CheckRow, classify_failure,
        )
        row = CheckRow(name="Unit Tests", bucket="fail",
                       state="failure", description="AssertionError in test_foo",
                       link="")
        assert classify_failure(row) == "code"

    def test_log_excerpt_can_override_description(self):
        """When description is generic but log shows infra phrase, infra wins."""
        from axiom.extensions.builtins.release.pr_check_watcher import (
            CheckRow, classify_failure,
        )
        row = CheckRow(name="Build", bucket="fail", state="failure",
                       description="failed", link="")
        assert classify_failure(row, log_excerpt="spending limit reached") == "infra"


# ---------------------------------------------------------------------------
# `gh` parsing
# ---------------------------------------------------------------------------


@pytest.fixture
def gh_runner(monkeypatch):
    """Replace `_run_gh` with a scripted reply queue.

    The skill calls `_run_gh(["pr", "list", "--author", "@me", "--json", ...])`
    and `_run_gh(["pr", "checks", "<n>", "--json", ...])` — tests pre-load
    JSON responses in order.
    """
    replies: list[str] = []
    calls: list[list[str]] = []

    def fake(args: list[str]) -> str:
        calls.append(args)
        if replies:
            return replies.pop(0)
        return ""

    from axiom.extensions.builtins.release import pr_check_watcher
    monkeypatch.setattr(pr_check_watcher, "_run_gh", fake)
    return calls, replies


class TestListUserPrs:
    def test_parses_gh_pr_list_json(self, gh_runner):
        from axiom.extensions.builtins.release.pr_check_watcher import (
            list_user_prs,
        )
        calls, replies = gh_runner
        replies.append(json.dumps([
            {"number": 211, "title": "feat(scheduling): ...",
             "url": "https://github.com/o/r/pull/211",
             "headRefName": "feat/schedule-install-multi-backend"},
            {"number": 199, "title": "fix: ...",
             "url": "https://github.com/o/r/pull/199",
             "headRefName": "fix/foo"},
        ]))

        prs = list_user_prs()
        assert [p.number for p in prs] == [211, 199]
        assert prs[0].title.startswith("feat(scheduling)")

    def test_empty_pr_list_returns_empty(self, gh_runner):
        from axiom.extensions.builtins.release.pr_check_watcher import (
            list_user_prs,
        )
        _, replies = gh_runner
        replies.append("[]")
        assert list_user_prs() == []

    def test_invokes_gh_with_author_me_filter(self, gh_runner):
        from axiom.extensions.builtins.release.pr_check_watcher import (
            list_user_prs,
        )
        calls, replies = gh_runner
        replies.append("[]")
        list_user_prs()
        assert calls[0][:5] == ["pr", "list", "--author", "@me", "--json"]


class TestFetchPrChecks:
    def test_parses_gh_pr_checks_json(self, gh_runner):
        from axiom.extensions.builtins.release.pr_check_watcher import (
            fetch_pr_checks,
        )
        calls, replies = gh_runner
        replies.append(json.dumps([
            {"name": "Lint", "bucket": "pass", "state": "success",
             "description": "Successful", "link": "https://.../lint"},
            {"name": "Build Wheel", "bucket": "fail", "state": "failure",
             "description": "spending limit needs to be increased",
             "link": "https://.../build"},
        ]))

        checks = fetch_pr_checks(
            pr_number=211, title="t", url="u", head_branch="b",
        )
        assert checks.pr_number == 211
        assert len(checks.rows) == 2
        assert checks.rows[1].name == "Build Wheel"
        assert checks.rows[1].is_failing
        assert checks.overall == "failing"

    def test_invokes_gh_with_pr_number(self, gh_runner):
        from axiom.extensions.builtins.release.pr_check_watcher import (
            fetch_pr_checks,
        )
        calls, replies = gh_runner
        replies.append("[]")
        fetch_pr_checks(pr_number=211, title="t", url="u", head_branch="b")
        # The pr number ends up in the gh argv.
        assert "211" in calls[0]


# ---------------------------------------------------------------------------
# State-flip detection
# ---------------------------------------------------------------------------


class TestStateFlipDetection:
    def test_new_pr_failing_emits_flip(self, tmp_path: Path):
        """First time we see a PR and it's failing — that's a flip from
        'unknown' to 'failing' (we hadn't seen it before)."""
        from axiom.extensions.builtins.release.pr_check_watcher import (
            CheckRow, PRChecks, detect_state_flips,
        )
        current = [PRChecks(
            pr_number=211, title="t", url="u", head_branch="b",
            rows=[CheckRow("Build", "fail", "failure",
                           "spending limit", "")],
        )]
        flips = detect_state_flips(current, tmp_path / "pr-checks.json")
        assert len(flips) == 1
        assert flips[0].pr_number == 211
        assert flips[0].to_state == "failing"
        assert flips[0].classification == "infra"

    def test_passing_to_failing_emits_flip_with_classification(
        self, tmp_path: Path,
    ):
        from axiom.extensions.builtins.release.pr_check_watcher import (
            CheckRow, PRChecks, detect_state_flips,
        )
        state_path = tmp_path / "pr-checks.json"
        state_path.write_text(json.dumps({"211": {"overall": "passing"}}))

        current = [PRChecks(
            pr_number=211, title="t", url="u", head_branch="b",
            rows=[CheckRow("Tests", "fail", "failure",
                           "AssertionError", "")],
        )]
        flips = detect_state_flips(current, state_path)
        assert len(flips) == 1
        assert flips[0].from_state == "passing"
        assert flips[0].to_state == "failing"
        assert flips[0].classification == "code"

    def test_no_flip_when_state_unchanged(self, tmp_path: Path):
        from axiom.extensions.builtins.release.pr_check_watcher import (
            CheckRow, PRChecks, detect_state_flips,
        )
        state_path = tmp_path / "pr-checks.json"
        state_path.write_text(json.dumps({"211": {"overall": "passing"}}))

        current = [PRChecks(
            pr_number=211, title="t", url="u", head_branch="b",
            rows=[CheckRow("Lint", "pass", "success", "", "")],
        )]
        flips = detect_state_flips(current, state_path)
        assert flips == []

    def test_failing_to_passing_emits_recovery_flip(self, tmp_path: Path):
        """Recovery is a state-flip too — useful for closing 'still red?'
        loops in the operator's head."""
        from axiom.extensions.builtins.release.pr_check_watcher import (
            CheckRow, PRChecks, detect_state_flips,
        )
        state_path = tmp_path / "pr-checks.json"
        state_path.write_text(json.dumps({"211": {"overall": "failing"}}))

        current = [PRChecks(
            pr_number=211, title="t", url="u", head_branch="b",
            rows=[CheckRow("Lint", "pass", "success", "", "")],
        )]
        flips = detect_state_flips(current, state_path)
        assert len(flips) == 1
        assert flips[0].from_state == "failing"
        assert flips[0].to_state == "passing"

    def test_pending_state_does_not_emit_flip(self, tmp_path: Path):
        """Pending is in-flight, not a terminal state — don't notify on
        every poll while a run is mid-execution."""
        from axiom.extensions.builtins.release.pr_check_watcher import (
            CheckRow, PRChecks, detect_state_flips,
        )
        state_path = tmp_path / "pr-checks.json"
        state_path.write_text(json.dumps({"211": {"overall": "passing"}}))

        current = [PRChecks(
            pr_number=211, title="t", url="u", head_branch="b",
            rows=[CheckRow("Tests", "pending", "in_progress", "", "")],
        )]
        flips = detect_state_flips(current, state_path)
        assert flips == []

    def test_persists_state_after_run(self, tmp_path: Path):
        from axiom.extensions.builtins.release.pr_check_watcher import (
            CheckRow, PRChecks, detect_state_flips,
        )
        state_path = tmp_path / "pr-checks.json"
        current = [PRChecks(
            pr_number=211, title="t", url="u", head_branch="b",
            rows=[CheckRow("Lint", "pass", "success", "", "")],
        )]
        detect_state_flips(current, state_path)
        assert state_path.exists()
        saved = json.loads(state_path.read_text())
        assert saved["211"]["overall"] == "passing"


# ---------------------------------------------------------------------------
# Top-level integration
# ---------------------------------------------------------------------------


class TestWatchUserPrs:
    def test_end_to_end_with_failing_pr(self, tmp_path: Path, gh_runner):
        from axiom.extensions.builtins.release.pr_check_watcher import (
            watch_user_prs,
        )
        _, replies = gh_runner
        # First call: list PRs
        replies.append(json.dumps([
            {"number": 211, "title": "feat: x",
             "url": "https://...", "headRefName": "feat/x"},
        ]))
        # Second call: checks for PR 211
        replies.append(json.dumps([
            {"name": "Build Wheel", "bucket": "fail",
             "state": "failure",
             "description": "spending limit needs to be increased",
             "link": ""},
        ]))

        flips = watch_user_prs(state_dir=tmp_path)
        assert len(flips) == 1
        assert flips[0].pr_number == 211
        assert flips[0].classification == "infra"
