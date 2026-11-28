"""RIVET's sticky comment leads with one branded status strip a person reads at a glance.

Acceptance (the strip's own rows are RN-5 in test_rivet_note.py):
  RC-1 The strip says, in a word, who acts next on this pull request.
  RC-2 The checks bar counts passed, failing and running checks.
  RC-3 Outcomes and reports are grouped by who acts next (the counts feed the run summary).
  RC-4 The strip carries Axiom's mark and accent and follows light and dark.
  RC-5 Status colours are appkit's status tokens; the accent is never a status.
  RC-6 Text in a chart is escaped.
  RC-7 A chart is embedded by an image URL a repository member's browser can load, with
       alt text that says the same thing as the chart.
"""

from __future__ import annotations

import sys
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from rivet_charts import (  # noqa: E402
    ACCENT,
    STATUS,
    chart_block,
    chart_path,
    chart_url,
    check_counts,
    counts_from_outcomes,
    counts_from_report,
    counts_from_text,
    embed_markdown,
    group_for_outcome,
    group_from_report,
    group_from_text,
    status_strip_svg,
)


def _parse(svg: str) -> ET.Element:
    return ET.fromstring(svg)


class StatusStrip(unittest.TestCase):
    def test_rc1_names_who_acts_next_in_a_word(self) -> None:
        svg = status_strip_svg(pr=2225, word="Needs you", tone="warn", passed=12, failing=0, running=0)
        _parse(svg)
        self.assertIn("Needs you", svg)

    def test_rc2_counts_each_kind_of_check(self) -> None:
        svg = status_strip_svg(pr=1, word="Stalled", tone="warn", passed=9, failing=2, running=3)
        self.assertIn("9 passed", svg)
        self.assertIn("2 failing", svg)
        self.assertIn("3 running", svg)

    def test_rc4_brand_and_theme(self) -> None:
        svg = status_strip_svg(pr=1, word="Ready to land", tone="ok", passed=1, failing=0, running=1)
        self.assertIn(ACCENT, svg)
        self.assertIn("RIVET", svg)
        self.assertIn("prefers-color-scheme: dark", svg)


class Backlog(unittest.TestCase):
    PAYLOAD = {
        "outcomes": [
            {"number": 1, "status": "landed", "reason": ""},
            {"number": 2, "status": "waiting", "reason": "draft"},
            {"number": 3, "status": "escalated", "reason": "protected:security"},
            {"number": 4, "status": "waiting", "reason": "handoff"},
            {"number": 5, "status": "waiting", "reason": "checks not green"},
            {"number": 6, "status": "skipped", "reason": "budget-stop: only release pull requests land"},
            {"number": 7, "status": "waiting", "reason": "waiting on ada"},
        ]
    }

    def test_rc3_groups_outcomes_by_who_acts_next(self) -> None:
        self.assertEqual(group_for_outcome({"status": "escalated", "reason": "x"}), "needs_you")
        self.assertEqual(group_for_outcome({"status": "waiting", "reason": "draft"}), "author")
        counts = counts_from_outcomes(self.PAYLOAD)
        self.assertEqual(counts["landed"], 1)
        self.assertEqual(counts["author"], 1)
        self.assertEqual(counts["needs_you"], 2)
        self.assertEqual(counts["rivet"], 2)
        self.assertEqual(counts["checks"], 1)

    def test_rc3_reads_the_text_report_too(self) -> None:
        text = (
            "ready to land (2):\n  #1 a\n  #2 b\n\nneeds a person (3):\n  #3\n  #4\n  #5\n\n"
            "RIVET is handling (1):\n  #6\n\nwith the author (4):\n\nwaiting on CI (2):\n  #9\n"
        )
        counts = counts_from_text(text)
        self.assertEqual(
            counts, {"ready": 2, "needs_you": 3, "rivet": 1, "author": 4, "checks": 2, "landed": 0}
        )


class ThisPullRequest(unittest.TestCase):
    def test_rc8_finds_this_pull_request_in_the_text_report(self) -> None:
        text = (
            "ready to land (1):\n  #11 b passing approval=none t\n\n"
            "needs a person (1):\n  #12 try-cap t\n\n"
            "RIVET is handling (1):\n  #13 failing_checks t\n\n"
            "with the author (1):\n  #14 draft t\n\nwaiting on CI (1):\n  #15 b\n"
        )
        self.assertEqual(group_from_text(text, 11), "ready")
        self.assertEqual(group_from_text(text, 12), "needs_you")
        self.assertEqual(group_from_text(text, 13), "rivet")
        self.assertEqual(group_from_text(text, 14), "author")
        self.assertEqual(group_from_text(text, 15), "checks")
        self.assertEqual(group_from_text(text, 99), "checks")


class JsonReport(unittest.TestCase):
    REPORT = {
        "ready": [{"number": 1}],
        "needs_human": [{"number": 2}, {"number": 3}],
        "rivet_handling": [{"number": 4}],
        "with_author": [{"number": 5}],
        "waiting_on_ci": [{"number": 6}],
    }

    def test_rc9_counts_and_group_from_the_json_report(self) -> None:
        counts = counts_from_report(self.REPORT)
        self.assertEqual((counts["ready"], counts["needs_you"], counts["rivet"], counts["author"],
                          counts["checks"]), (1, 2, 1, 1, 1))
        self.assertEqual(group_from_report(self.REPORT, 3), "needs_you")
        self.assertEqual(group_from_report(self.REPORT, 5), "author")
        self.assertEqual(group_from_report(self.REPORT, 99), "checks")

    def test_rc10_check_counts(self) -> None:
        rollup = [{"conclusion": "SUCCESS"}, {"conclusion": "FAILURE"}, {"conclusion": "SKIPPED"},
                  {"status": "IN_PROGRESS", "conclusion": ""}, {"state": "SUCCESS"}]
        self.assertEqual(check_counts(rollup), (2, 1, 1))


class ContentAddressed(unittest.TestCase):
    def test_rc11_an_unchanged_chart_keeps_its_path(self) -> None:
        a = chart_path(7, "status", "<svg>a</svg>")
        self.assertEqual(a, chart_path(7, "status", "<svg>a</svg>"))
        self.assertNotEqual(a, chart_path(7, "status", "<svg>b</svg>"))
        seen = []
        block = chart_block(repo="o/r", pr=7, head_sha="abc", group="ready", passed=1, failing=0,
                            running=0, counts={"ready": 1},
                            publish_fn=lambda repo, path, svg: seen.append(path) or f"https://x/{path}")
        self.assertEqual(len(seen), 1)
        self.assertIn("https://x/charts/pr-7/status-", block)


class Palette(unittest.TestCase):
    def test_rc5_status_colours_are_appkit_tokens_and_not_the_accent(self) -> None:
        tokens = {"ok": "#0d7762", "warn": "#8a6d00", "bad": "#a91830", "info": "#1f6f8b", "none": "#6b737c"}
        for name, light in tokens.items():
            self.assertEqual(STATUS[name][0], light)
        self.assertNotIn(ACCENT.lower(), {pair[0].lower() for pair in STATUS.values()})
        self.assertNotIn(ACCENT.lower(), {pair[1].lower() for pair in STATUS.values()})


class Safety(unittest.TestCase):
    def test_rc6_text_is_escaped(self) -> None:
        svg = status_strip_svg(pr=1, word="<script>&", tone="ok", passed=1, failing=0, running=0)
        _parse(svg)
        self.assertNotIn("<script>", svg)


class Embedding(unittest.TestCase):
    def test_rc7_url_and_alt_text(self) -> None:
        url = chart_url("acme/widgets", "charts/pr-7/status-abc.svg")
        self.assertEqual(
            url, "https://github.com/acme/widgets/blob/rivet-charts/charts/pr-7/status-abc.svg?raw=true"
        )
        md = embed_markdown(url, alt="RIVET: ✓ Ready to land", link="https://x/pull/7/checks")
        self.assertIn('alt="RIVET: ✓ Ready to land"', md)
        self.assertIn('href="https://x/pull/7/checks"', md)
        self.assertIn(url, md)


if __name__ == "__main__":
    unittest.main()
