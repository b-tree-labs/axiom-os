"""The do-not-merge Hold: comment is required and labels are never removed."""

from __future__ import annotations

import os
import sys
import unittest
from datetime import UTC, date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from rivet_hold_check import (  # noqa: E402
    Comment,
    Pull,
    evaluate,
    missing_hold_message,
    parse_hold_body,
    resolve_hold,
    summary_markdown,
    write_summary,
)

WHEN = datetime(2026, 10, 26, 11, 30, tzinfo=UTC)


def _comment(body: str, at: datetime = WHEN) -> Comment:
    return Comment(body=body, created_at=at)


class RivetHoldCheckTest(unittest.TestCase):
    def test_default_review_by_is_seven_days_after_the_comment(self) -> None:
        parsed, err = parse_hold_body(
            "Hold: waiting on the DNS cutover\nReleases: octo-owner\n",
            WHEN,
        )
        self.assertIsNone(err)
        assert parsed is not None
        reason, releases, review_by = parsed
        self.assertEqual(reason, "waiting on the DNS cutover")
        self.assertEqual(releases, "octo-owner")
        self.assertEqual(review_by, date(2026, 11, 2))

    def test_explicit_review_by_overrides_the_default(self) -> None:
        parsed, err = parse_hold_body(
            "\nHold: proxy stays draft\n"
            "Releases: Octo marks the pull request ready\n"
            "Review-by: 2026-12-01\n",
            WHEN,
        )
        self.assertIsNone(err)
        assert parsed is not None
        self.assertEqual(parsed[2], date(2026, 12, 1))

    def test_a_hold_comment_needs_a_reason_and_a_release(self) -> None:
        _parsed, empty_reason = parse_hold_body("Hold:\nReleases: Octo\n", WHEN)
        self.assertIn("reason", empty_reason or "")
        _parsed, missing_release = parse_hold_body("Hold: because\n", WHEN)
        self.assertIn("Releases:", missing_release or "")
        _parsed, bad_date = parse_hold_body(
            "Hold: because\nReleases: Octo\nReview-by: next week\n",
            WHEN,
        )
        self.assertIn("YYYY-MM-DD", bad_date or "")

    def test_text_that_does_not_start_with_hold_is_ignored(self) -> None:
        parsed, err = parse_hold_body("Please hold this until Friday.", WHEN)
        self.assertIsNone(parsed)
        self.assertIsNone(err)

    def test_the_latest_hold_comment_wins(self) -> None:
        earlier = _comment("Hold: first reason\nReleases: Ada\n", WHEN)
        later = _comment(
            "Hold: second reason\nReleases: the runbook\nReview-by: 2026-11-15\n",
            datetime(2026, 10, 27, 1, 0, tzinfo=UTC),
        )
        parsed, err = resolve_hold((later, earlier))
        self.assertIsNone(err)
        assert parsed is not None
        self.assertEqual(parsed[0], "second reason")
        self.assertEqual(parsed[1], "the runbook")
        self.assertEqual(parsed[2], date(2026, 11, 15))

    def test_an_invalid_latest_comment_does_not_fall_back(self) -> None:
        good = _comment("Hold: first reason\nReleases: Ada\n", WHEN)
        bad = _comment("Hold:\n", datetime(2026, 10, 28, tzinfo=UTC))
        parsed, err = resolve_hold((good, bad))
        self.assertIsNone(parsed)
        self.assertIn("reason", err or "")

    def test_missing_comment_fails_and_asks_for_one(self) -> None:
        pull = Pull(2713, "proxy", "https://example.test/2713", ("do-not-merge",), ())
        report = evaluate((pull,), today=date(2026, 10, 26), focus=None)
        self.assertEqual(len(report.failures), 1)
        self.assertIn("Hold:", report.failures[0])
        self.assertIn("Releases:", report.failures[0])
        self.assertIn("does not remove the label", report.failures[0])
        self.assertIn("no comment starts with 'Hold:'", report.failures[0])
        self.assertEqual(report.overdue, ())

    def test_a_valid_hold_is_not_a_failure(self) -> None:
        pull = Pull(
            9,
            "docs",
            "https://example.test/9",
            ("do-not-merge",),
            (_comment("Hold: Octo merges the workflow\nReleases: octo-owner\n"),),
        )
        report = evaluate((pull,), today=date(2026, 10, 26), focus=9)
        self.assertEqual(report.failures, ())
        self.assertEqual(report.overdue, ())

    def test_past_review_by_is_listed_and_does_not_fail(self) -> None:
        pull = Pull(
            4,
            "old hold",
            "https://example.test/4",
            ("do-not-merge",),
            (_comment("Hold: still waiting\nReleases: the DNS change\nReview-by: 2026-10-01\n"),),
        )
        report = evaluate((pull,), today=date(2026, 10, 26), focus=None)
        self.assertEqual(report.failures, ())
        self.assertEqual(len(report.overdue), 1)
        self.assertEqual(report.overdue[0].review_by, date(2026, 10, 1))
        self.assertIn("does not remove labels", report.summary)
        self.assertIn("#4", report.summary)
        self.assertIn("2026-10-01", report.summary)
        self.assertIn("still waiting", report.summary)

    def test_review_by_today_is_not_past(self) -> None:
        pull = Pull(
            5,
            "due today",
            "",
            ("do-not-merge",),
            (_comment("Hold: due today\nReleases: Octo\nReview-by: 2026-10-26\n"),),
        )
        report = evaluate((pull,), today=date(2026, 10, 26), focus=5)
        self.assertEqual(report.overdue, ())

    def test_a_pull_without_the_label_is_ignored(self) -> None:
        pull = Pull(8, "open", "", (), ())
        report = evaluate((pull,), today=date(2026, 10, 26), focus=None)
        self.assertEqual(report.failures, ())
        self.assertIn("None.", report.summary)

    def test_another_pull_request_does_not_fail_this_run(self) -> None:
        other = Pull(2713, "proxy", "https://example.test/2713", ("do-not-merge",), ())
        report = evaluate((other,), today=date(2026, 10, 26), focus=2716)
        self.assertEqual(report.failures, ())

    def test_summary_is_one_block(self) -> None:
        text = summary_markdown(())
        self.assertEqual(text.count("## do-not-merge holds past review-by"), 1)

    def test_the_script_never_removes_labels(self) -> None:
        source = (
            Path(__file__).resolve().parents[1] / "scripts" / "rivet_hold_check.py"
        ).read_text(encoding="utf-8")
        for banned in ("remove-label", "label delete", "labels/", "--method", "DELETE"):
            self.assertNotIn(banned, source)

    def test_message_asks_for_the_comment(self) -> None:
        text = missing_hold_message(12, "no comment starts with 'Hold:'")
        self.assertIn("Add a pull request comment that starts with 'Hold:'", text)

    def test_summary_file_appends_once(self) -> None:
        path = Path(os.environ.get("TMPDIR", "/tmp")) / "rivet-hold-summary.md"
        path.write_text("existing\n", encoding="utf-8")
        previous = os.environ.get("GITHUB_STEP_SUMMARY")
        os.environ["GITHUB_STEP_SUMMARY"] = str(path)
        try:
            write_summary("## do-not-merge holds past review-by\n\nNone.\n")
        finally:
            if previous is None:
                os.environ.pop("GITHUB_STEP_SUMMARY", None)
            else:
                os.environ["GITHUB_STEP_SUMMARY"] = previous
        written = path.read_text(encoding="utf-8")
        self.assertIn("existing", written)
        self.assertEqual(written.count("## do-not-merge holds past review-by"), 1)
        path.unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main()
