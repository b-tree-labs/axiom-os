"""RIVET's note on a pull request tells the truth, stays current, and links to the exact place.

#2022's note read "RIVET is handling this" for a week while nothing handled it, never
changed, drew a repository-wide chart frozen at its last edit, and linked every picture
to a generic page. Acceptance:

  RN-1  "Handling" is claimed only with evidence in the last 24 hours (a fix attempt or
        a branch update). With none, a failing PR is "stalled" and the note names who acts.
  RN-2  The note says when it was written ("as of ..."), and an open note older than
        two hours is rewritten even when its verdict key is unchanged.
  RN-3  A merged or closed pull request's note is rewritten once to say so, then left alone.
  RN-4  Every failing check is a link to its own job; the next steps are links that go to
        the exact page (the failed run, the merge box, the review page).
  RN-5  The status strip is one calm image: fixed height, two type sizes, no animation, no
        repository-wide backlog in a per-PR note.
  RN-6  The headline never says "RIVET: RIVET".
  RN-7  A check superseded by a newer run of the same check is not counted as failing.
"""

from __future__ import annotations

import re
import sys
import unittest
import xml.etree.ElementTree as ET
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import rivet_charts  # noqa: E402
import rivet_note as N  # noqa: E402

NOW = datetime(2026, 11, 23, 21, 0, tzinfo=UTC)
REPO = "o/r"


def facts(**kw):
    base = dict(
        repo=REPO, number=7, now=NOW, state="open",
        run_url="https://github.com/o/r/actions/runs/99",
        failing=[N.Check("Unit Tests (3.13)", "https://github.com/o/r/actions/runs/5/job/6")],
        passed=8, running=0, last_action=None, merged_at=None, merged_by="", merge_sha="",
    )
    base.update(kw)
    return N.Facts(**base)


class V:  # the verdict fields the note reads
    def __init__(self, state="rivet", why=("A check is failing, and RIVET is trying to repair it.",),
                 who="RIVET", next_steps=("Nothing yet.",), key="k1", draft=False, number=7):
        self.state, self.why, self.who, self.next_steps = state, why, who, next_steps
        self.key, self.draft, self.number = key, draft, number


class HonestStateTest(unittest.TestCase):
    def test_rn1_handling_needs_evidence(self):
        self.assertEqual(N.honest_state("rivet", facts()), "stalled")

    def test_rn1_a_recent_action_is_evidence(self):
        act = N.Action(at=NOW - timedelta(hours=3), what="updated the branch from main", url="https://x/c")
        self.assertEqual(N.honest_state("rivet", facts(last_action=act)), "rivet")

    def test_rn1_an_old_action_is_not(self):
        act = N.Action(at=NOW - timedelta(hours=30), what="updated the branch from main", url="https://x/c")
        self.assertEqual(N.honest_state("rivet", facts(last_action=act)), "stalled")

    def test_other_states_pass_through(self):
        self.assertEqual(N.honest_state("lands", facts()), "lands")

    def test_rn1_stalled_names_who_acts(self):
        body = N.render(V(), facts())
        self.assertNotIn("is handling", body.lower())
        self.assertIn("nothing is repairing it", body.lower())
        self.assertIn("**Who acts next:** the author", body)
        self.assertNotIn("trying to repair", body)
        self.assertNotIn("next pass", body)  # no promise RIVET does not keep yet
        self.assertIn("no action on this pull request in the last 24 hours", body)

    def test_rn6_no_double_rivet(self):
        act = N.Action(at=NOW - timedelta(hours=1), what="updated the branch from main", url="https://x/c")
        for fa in (facts(), facts(last_action=act)):
            self.assertNotIn("RIVET: RIVET", N.render(V(), fa))


class FreshnessTest(unittest.TestCase):
    def test_rn2_the_note_says_when(self):
        self.assertIn("as of 2026-11-23 21:00 UTC", N.render(V(), facts()))

    def test_rn2_an_old_open_note_is_rewritten_with_the_same_key(self):
        old = NOW - timedelta(hours=3)
        self.assertTrue(N.needs_rewrite(existing_key="k1", new_key="k1", updated_at=old, now=NOW, state="open"))

    def test_rn2_a_fresh_note_with_the_same_key_is_left_alone(self):
        new = NOW - timedelta(minutes=20)
        self.assertFalse(N.needs_rewrite(existing_key="k1", new_key="k1", updated_at=new, now=NOW, state="open"))

    def test_rn2_a_changed_key_is_always_rewritten(self):
        new = NOW - timedelta(minutes=1)
        self.assertTrue(N.needs_rewrite(existing_key="k1", new_key="k2", updated_at=new, now=NOW, state="open"))

    def test_rn3_a_merged_note_is_rewritten_once(self):
        fa = facts(state="merged", merged_at=NOW - timedelta(hours=1), merged_by="ben", merge_sha="abc1234def")
        body = N.render(V(state="lands"), fa)
        self.assertIn("Merged", body)
        self.assertIn("commit/abc1234def", body)
        key = N.terminal_key("merged")
        self.assertFalse(N.needs_rewrite(existing_key=key, new_key=key, updated_at=NOW - timedelta(days=9), now=NOW, state="merged"))
        self.assertTrue(N.needs_rewrite(existing_key="k1", new_key=key, updated_at=NOW, now=NOW, state="merged"))


class LinksTest(unittest.TestCase):
    def test_rn4_each_failing_check_links_to_its_own_job(self):
        two = [N.Check("Lint", "https://github.com/o/r/actions/runs/5/job/1"),
               N.Check("Unit Tests (3.13)", "https://github.com/o/r/actions/runs/5/job/2")]
        body = N.render(V(), facts(failing=two))
        self.assertIn("[Lint](https://github.com/o/r/actions/runs/5/job/1)", body)
        self.assertIn("[Unit Tests (3.13)](https://github.com/o/r/actions/runs/5/job/2)", body)

    def test_rn4_next_steps_are_links_to_the_exact_page(self):
        body = N.render(V(), facts())
        self.assertIn("https://github.com/o/r/actions/runs/5", body)       # the failed run, to re-run
        self.assertIn("https://github.com/o/r/pull/7#partial-pull-merging", body)  # the merge box

    def test_rn4_a_pull_request_number_in_a_reason_links(self):
        body = N.render(V(state="superseded", why=("Superseded by #12.",), who="the author"), facts())
        self.assertIn("[#12](https://github.com/o/r/pull/12)", body)

    def test_rn4_the_strip_links_to_the_first_failing_job(self):
        self.assertEqual(N.strip_link(facts()), "https://github.com/o/r/actions/runs/5/job/6")
        self.assertEqual(N.strip_link(facts(failing=[])), "https://github.com/o/r/pull/7/checks")


class StripTest(unittest.TestCase):
    def setUp(self):
        self.svg = rivet_charts.status_strip_svg(pr=7, word="Stalled", tone="warn", passed=8, failing=1, running=0)

    def test_rn5_it_is_valid_svg_of_a_fixed_height(self):
        root = ET.fromstring(self.svg)
        self.assertEqual(root.get("height"), "56")

    def test_rn5_two_type_sizes_and_no_animation(self):
        sizes = set(re.findall(r'font-size="(\d+)"', self.svg))
        self.assertLessEqual(sizes, {"12", "14"})
        self.assertNotIn("animation", self.svg)
        self.assertNotIn("@keyframes", self.svg)

    def test_rn5_a_per_pr_block_has_one_image_and_no_backlog(self):
        published = []
        block = rivet_charts.chart_block(
            repo=REPO, pr=7, head_sha="abc", group="rivet", passed=8, failing=1, running=0,
            counts={"needs_you": 8}, publish_fn=lambda r, p, s: published.append(p) or f"https://img/{p}",
            word="Stalled", tone="warn", link="https://github.com/o/r/actions/runs/5/job/6")
        self.assertEqual(len(published), 1)
        self.assertEqual(block.count("<img"), 1)
        self.assertNotIn("/pulls", block)
        self.assertIn("actions/runs/5/job/6", block)


class CountsTest(unittest.TestCase):
    def test_rn7_a_superseded_cancelled_run_is_not_a_failure(self):
        rows = [
            {"name": "Unit Tests (3.13)", "workflowName": "CI", "conclusion": "CANCELLED", "status": "COMPLETED",
             "completedAt": "2026-11-23T10:00:00Z", "startedAt": "2026-11-23T09:50:00Z"},
            {"name": "Unit Tests (3.13)", "workflowName": "CI", "conclusion": "SUCCESS", "status": "COMPLETED",
             "completedAt": "2026-11-23T10:30:00Z", "startedAt": "2026-11-23T10:05:00Z"},
        ]
        self.assertEqual(rivet_charts.check_counts(rows), (1, 0, 0))


if __name__ == "__main__":
    unittest.main()
