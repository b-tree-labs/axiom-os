# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tests for `pr_check_responder` — Layer 3 of the RIVET PR-CI watcher.

The watcher (slice 18) returns `StateFlip` events. The responder
routes them:

  - infra flip   → high-urgency notification ("ACTION REQUIRED")
  - code flip    → normal-urgency notification ("FIX NEEDED") plus a
                    failure-report markdown under
                    `~/.axi/agents/rivet/reports/`
  - recovery     → low-urgency notification ("recovered")

Conservative cut: NO destructive ops yet. No auto-branch-spawn (a
wrong auto-commit on the user's branch is worse than the missed
notification we're trying to fix). No auto-issue-close (slice 20).
Surface only, structured.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


# ---------------------------------------------------------------------------
# Test double for NotificationProvider
# ---------------------------------------------------------------------------


@dataclass
class CapturedNotification:
    recipients: list[str]
    subject: str
    body: str
    urgency: str


@dataclass
class FakeSink:
    sent: list[CapturedNotification] = field(default_factory=list)

    def send(
        self,
        recipients: list[str],
        subject: str,
        body: str,
        urgency: str = "normal",
    ) -> bool:
        self.sent.append(CapturedNotification(
            recipients=recipients, subject=subject, body=body, urgency=urgency,
        ))
        return True


def _make_flip(
    *,
    to_state: str,
    classification: str = "code",
    from_state: str = "passing",
    pr_number: int = 211,
    failing_jobs: list[tuple[str, str, str]] | None = None,
):
    from axiom.extensions.builtins.release.pr_check_watcher import (
        CheckRow, StateFlip,
    )
    failing_rows = [
        CheckRow(name=n, bucket="fail", state="failure",
                 description=d, link=link)
        for (n, d, link) in (failing_jobs or [])
    ]
    return StateFlip(
        pr_number=pr_number,
        title="feat: a thing",
        url=f"https://github.com/o/r/pull/{pr_number}",
        head_branch="feat/x",
        from_state=from_state,
        to_state=to_state,
        failing_rows=failing_rows,
        classification=classification,
    )


# ---------------------------------------------------------------------------
# Infra flips — action-required notification, NO report file
# ---------------------------------------------------------------------------


class TestInfraFlip:
    def test_infra_flip_sends_high_urgency_action_required(self, tmp_path: Path):
        from axiom.extensions.builtins.release.pr_check_responder import (
            handle_flip,
        )
        sink = FakeSink()
        flip = _make_flip(
            to_state="failing", classification="infra",
            failing_jobs=[("Build Wheel", "spending limit", "https://.../bw")],
        )
        report = handle_flip(flip, state_dir=tmp_path, sink=sink)

        assert report is None  # no fix branch for infra
        assert len(sink.sent) == 1
        n = sink.sent[0]
        assert n.urgency == "high"
        assert "ACTION REQUIRED" in n.subject
        assert "#211" in n.subject
        assert "Build Wheel" in n.body
        assert "spending limit" in n.body
        assert "https://.../bw" in n.body

    def test_infra_flip_does_not_write_report(self, tmp_path: Path):
        from axiom.extensions.builtins.release.pr_check_responder import (
            handle_flip,
        )
        sink = FakeSink()
        handle_flip(
            _make_flip(to_state="failing", classification="infra",
                       failing_jobs=[("Build", "billing", "")]),
            state_dir=tmp_path, sink=sink,
        )
        reports_dir = tmp_path / "agents" / "rivet" / "reports"
        # Either the dir doesn't exist or it's empty — both OK
        if reports_dir.exists():
            assert list(reports_dir.iterdir()) == []


# ---------------------------------------------------------------------------
# Code flips — fix-needed notification + report file
# ---------------------------------------------------------------------------


class TestCodeFlip:
    def test_code_flip_sends_normal_urgency_fix_needed(self, tmp_path: Path):
        from axiom.extensions.builtins.release.pr_check_responder import (
            handle_flip,
        )
        sink = FakeSink()
        flip = _make_flip(
            to_state="failing", classification="code",
            failing_jobs=[("Unit Tests (3.13)", "AssertionError", "https://.../ut13")],
        )
        report = handle_flip(flip, state_dir=tmp_path, sink=sink)

        assert report is not None
        assert len(sink.sent) == 1
        n = sink.sent[0]
        assert n.urgency == "normal"
        assert "FIX NEEDED" in n.subject
        assert "#211" in n.subject

    def test_code_flip_writes_report_under_reports_dir(self, tmp_path: Path):
        from axiom.extensions.builtins.release.pr_check_responder import (
            handle_flip,
        )
        sink = FakeSink()
        flip = _make_flip(
            to_state="failing", classification="code",
            failing_jobs=[("Unit Tests", "AssertionError in test_foo",
                           "https://.../ut")],
        )
        report = handle_flip(flip, state_dir=tmp_path, sink=sink)

        assert report is not None
        assert report.pr_number == 211
        assert report.path.exists()
        assert report.path.parent == tmp_path / "agents" / "rivet" / "reports"
        body = report.path.read_text()
        # Report references the PR + the failing job
        assert "211" in body
        assert "Unit Tests" in body
        assert "AssertionError in test_foo" in body
        assert "https://.../ut" in body

    def test_code_flip_notification_links_to_report_path(self, tmp_path: Path):
        """The notification body should tell the user where the report
        lives so they can act on it without going hunting."""
        from axiom.extensions.builtins.release.pr_check_responder import (
            handle_flip,
        )
        sink = FakeSink()
        report = handle_flip(
            _make_flip(to_state="failing", classification="code",
                       failing_jobs=[("Unit Tests", "boom", "")]),
            state_dir=tmp_path, sink=sink,
        )
        assert str(report.path) in sink.sent[0].body


# ---------------------------------------------------------------------------
# Recovery flips — low-urgency notification, no report
# ---------------------------------------------------------------------------


class TestRecoveryFlip:
    def test_recovery_flip_sends_low_urgency(self, tmp_path: Path, monkeypatch):
        """Recovery emits a low-urgency notification. The auto-closer
        is invoked but stubbed to return no matches (network isolation
        — the responder unit tests should not depend on real `gh`
        state)."""
        from axiom.extensions.builtins.release import pr_check_auto_closer
        from axiom.extensions.builtins.release.pr_check_responder import (
            handle_flip,
        )
        monkeypatch.setattr(pr_check_auto_closer, "_run_gh", lambda args: "")
        sink = FakeSink()
        report = handle_flip(
            _make_flip(to_state="passing", from_state="failing",
                       classification="unknown"),
            state_dir=tmp_path, sink=sink,
        )
        assert report is None
        assert len(sink.sent) == 1
        n = sink.sent[0]
        assert n.urgency == "low"
        assert "recovered" in n.subject.lower()

    def test_recovery_flip_appends_auto_closed_section(
        self, tmp_path: Path, monkeypatch,
    ):
        """When the auto-closer closes stale issues, the recovery
        notification body shows what was closed so the operator has
        an audit trail."""
        from axiom.extensions.builtins.release import pr_check_auto_closer
        from axiom.extensions.builtins.release.pr_check_responder import (
            handle_flip,
        )

        import json
        # Two stale 🔴 issues for PR 197, then close calls return empty.
        responses = [
            json.dumps([
                {"number": 199,
                 "title": "🔴 CI failed on `refs/pull/197/merge` (abc)",
                 "author": {"login": "app/github-actions", "is_bot": True},
                 "state": "OPEN"},
                {"number": 198,
                 "title": "🔴 CI failed on `refs/pull/197/merge` (def)",
                 "author": {"login": "app/github-actions", "is_bot": True},
                 "state": "OPEN"},
            ]),
            "", "",  # close 199, close 198
        ]
        def fake_gh(args):
            return responses.pop(0) if responses else ""
        monkeypatch.setattr(pr_check_auto_closer, "_run_gh", fake_gh)

        sink = FakeSink()
        handle_flip(
            _make_flip(to_state="passing", from_state="failing",
                       pr_number=197, classification="unknown"),
            state_dir=tmp_path, sink=sink,
        )
        body = sink.sent[0].body
        assert "Auto-closed" in body
        assert "#199" in body and "#198" in body


# ---------------------------------------------------------------------------
# Bulk dispatch
# ---------------------------------------------------------------------------


class TestHandleFlips:
    def test_empty_returns_empty(self, tmp_path: Path):
        from axiom.extensions.builtins.release.pr_check_responder import (
            handle_flips,
        )
        sink = FakeSink()
        assert handle_flips([], state_dir=tmp_path, sink=sink) == []
        assert sink.sent == []

    def test_mixed_returns_only_code_reports(self, tmp_path: Path):
        from axiom.extensions.builtins.release.pr_check_responder import (
            handle_flips,
        )
        sink = FakeSink()
        flips = [
            _make_flip(to_state="failing", classification="infra",
                       pr_number=211,
                       failing_jobs=[("Build", "billing", "")]),
            _make_flip(to_state="failing", classification="code",
                       pr_number=199,
                       failing_jobs=[("Tests", "AssertionError", "")]),
            _make_flip(to_state="passing", from_state="failing",
                       pr_number=184),
        ]
        reports = handle_flips(flips, state_dir=tmp_path, sink=sink)

        # 3 notifications dispatched
        assert len(sink.sent) == 3
        # Only the code flip produced a report
        assert len(reports) == 1
        assert reports[0].pr_number == 199


# ---------------------------------------------------------------------------
# Report format
# ---------------------------------------------------------------------------


class TestReportFormat:
    def test_report_includes_actionable_next_steps_section(self, tmp_path: Path):
        """A report that doesn't tell the operator what to do next is
        just a log dump. Include a 'Next steps' section with concrete
        suggestions."""
        from axiom.extensions.builtins.release.pr_check_responder import (
            handle_flip,
        )
        sink = FakeSink()
        report = handle_flip(
            _make_flip(to_state="failing", classification="code",
                       failing_jobs=[("Lint", "ruff F401 unused import", "")]),
            state_dir=tmp_path, sink=sink,
        )
        body = report.path.read_text().lower()
        assert "next step" in body or "to fix" in body or "what to do" in body

    def test_report_lists_all_failing_jobs(self, tmp_path: Path):
        from axiom.extensions.builtins.release.pr_check_responder import (
            handle_flip,
        )
        sink = FakeSink()
        report = handle_flip(
            _make_flip(
                to_state="failing", classification="code",
                failing_jobs=[
                    ("Unit Tests (3.11)", "AssertionError test_a", "u1"),
                    ("Unit Tests (3.13)", "AssertionError test_b", "u2"),
                    ("Lint", "ruff E501", "l1"),
                ],
            ),
            state_dir=tmp_path, sink=sink,
        )
        body = report.path.read_text()
        for job in ("Unit Tests (3.11)", "Unit Tests (3.13)", "Lint"):
            assert job in body
