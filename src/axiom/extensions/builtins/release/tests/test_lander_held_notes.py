# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""A held pull request says that it is held, why, and who acts next (#2246).

Five green Dependabot pull requests sat for five days. The lander held each one,
correctly, because it changed dependency files, but nothing on them said so. The
only RIVET comment was the backlog of every open pull request, posted when each
one opened, in reason codes ("dependency tier=2"). These tests pin the fix:

- HN-1: a held pull request's note leads with its own verdict in plain words,
  names who acts next and what they do, and leaves out the rest of the backlog.
- HN-2: every reason code the report can give reads as a sentence; an unknown
  one still reads as one.
- HN-3: a refresh posts one note on a held pull request that has none, edits a
  note whose verdict changed, and leaves a current note alone; it never posts a
  note on a draft, or on a pull request nobody has to act on.
- HN-4: the run summary lists the held pull requests first, each with its reason
  and who acts, and counts the drafts instead of listing them.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

from axiom.extensions.builtins.release.lander import verdict as V


def _view(number, reasons=(), *, checks="passing", title=None, reviewer=""):
    return {
        "number": number,
        "title": title or f"change {number}",
        "head_sha": "abc",
        "head_ref": f"b{number}",
        "checks": checks,
        "approval": "",
        "reasons": list(reasons),
        "repair": "",
        "reviewer": reviewer,
        "tier": 2,
    }


def _report(**sections):
    base = {
        "repo": "o/r",
        "ready": [],
        "needs_human": [],
        "waiting_on_ci": [],
        "rivet_handling": [],
        "with_author": [],
        "superseded": [],
    }
    base.update(sections)
    return base


def _script():
    root = Path(__file__).resolve()
    while not (root / ".github").is_dir():
        root = root.parent
    spec = importlib.util.spec_from_file_location(
        "rivet_comment", root / ".github" / "scripts" / "rivet_warn_comment.py"
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


# -- HN-1: the note leads with this pull request's verdict ---------------------------


def test_hn1_a_dependency_hold_says_held_why_and_who_approves():
    report = _report(needs_human=[_view(2059, ["dependency"])], ready=[_view(7)])
    found = V.verdict_for(report, 2059)
    assert found is not None and found.state == "person"
    body = _script().comment_body("backlog text #7", verdict=found)
    first = body.split("\n", 2)[1]
    assert "Held" in first and "needs a person" in first
    assert "dependency files" in body
    assert "approve" in body.lower()
    # The backlog of other pull requests is not dumped on this one.
    assert "#7" not in body
    assert "tier=" not in body


def test_hn1_a_review_request_names_the_reviewer_as_who_acts():
    report = _report(needs_human=[_view(12, ["waiting on ada"])])
    found = V.verdict_for(report, 12)
    assert found.who == "ada"
    assert "ada" in found.next_steps[0]


def test_hn1_a_reviewer_who_also_approves_gets_one_step_not_two():
    found = V.verdict_for(_report(needs_human=[_view(13, ["dependency", "waiting on ada"])]), 13)
    assert len(found.next_steps) == 1
    assert "ada" in found.next_steps[0] and "approves" in found.next_steps[0]


def test_hn1_ready_waiting_rivet_and_author_each_read_plainly():
    report = _report(
        ready=[_view(1)],
        waiting_on_ci=[_view(2, checks="pending")],
        rivet_handling=[_view(3, ["failing_checks"], checks="failing")],
        with_author=[_view(4, ["draft"])],
        superseded=[{"number": 5, "keeper": 6, "reason": "same files"}],
    )
    assert "lands" in V.verdict_for(report, 1).headline
    assert "checks" in V.verdict_for(report, 2).headline.lower()
    assert "RIVET" in V.verdict_for(report, 3).headline
    assert "draft" in V.verdict_for(report, 4).headline.lower()
    assert "#6" in V.verdict_for(report, 5).headline
    assert V.verdict_for(report, 99) is None


def test_hn1_without_a_verdict_the_old_report_comment_stands():
    body = _script().comment_body("needs a person:\n  #4 waiting on ada\n")
    assert "#4 waiting on ada" in body


# -- HN-2: every reason reads as a sentence --------------------------------------------


def test_hn2_every_known_reason_has_plain_words():
    codes = (
        "dependency",
        "workflow",
        "test-setup",
        "data",
        "security",
        "pricing",
        "behavior",
        "draft",
        "conflict",
        "failing_checks",
        "try-cap",
        "checks_unknown",
        "approval_reset",
        "review_refused",
        "file_overlap:4,5",
        "waiting on ada",
        "changes requested by bo",
    )
    for code in codes:
        text = V.explain(code)
        assert text.endswith("."), code
        assert "_" not in text and "file_overlap" not in text and "try-cap" not in text, code


def test_hn2_an_unknown_reason_still_reads_as_a_sentence():
    text = V.explain("licence")
    assert "licence" in text and text.endswith(".")


# -- HN-3: refresh posts once, edits on change, never spams ---------------------------


def test_hn3_refresh_posts_once_edits_on_change_and_skips_the_rest():
    report = _report(
        needs_human=[
            _view(10, ["dependency"]),
            _view(11, ["conflict"]),
            _view(13, ["dependency", "draft"]),
        ],
        ready=[_view(14)],
        with_author=[_view(15, ["draft"])],
    )
    key10 = V.verdict_for(report, 10).key
    existing = {10: key10, 11: "stale-key"}
    plan = dict(V.refresh_plan(report, existing))
    assert 10 not in plan  # current note: left alone
    assert plan[11] == "edit"  # verdict changed: edited in place
    assert 13 not in plan  # draft: no new note
    assert 14 not in plan and 15 not in plan  # nobody has to act


def test_hn3_a_note_on_a_pull_request_nobody_must_act_on_is_still_kept_current():
    report = _report(ready=[_view(20)])
    plan = dict(V.refresh_plan(report, {20: "old-held-key"}))
    assert plan[20] == "edit"


def test_hn3_a_new_held_pull_request_gets_one_note():
    report = _report(needs_human=[_view(30, ["dependency"])])
    assert dict(V.refresh_plan(report, {})) == {30: "post"}


def test_hn3_the_key_rides_in_the_marker_so_a_rerun_matches():
    report = _report(needs_human=[_view(40, ["dependency"])])
    found = V.verdict_for(report, 40)
    body = _script().comment_body("", verdict=found)
    assert V.key_in(body) == found.key
    assert V.key_in("<!-- rivet-lander:warn-only -->\nold") == ""


# -- HN-4: the run summary --------------------------------------------------------------


def test_hn4_summary_lists_held_first_and_counts_drafts():
    report = _report(
        needs_human=[_view(50, ["dependency"], title="bump npm")],
        ready=[_view(51)],
        with_author=[_view(52, ["draft"]), _view(53, ["draft"])],
    )
    text = V.summary_markdown(report)
    held_at = text.index("Needs a person")
    assert held_at < text.index("Ready")
    assert "#50" in text and "dependency files" in text
    assert "2 drafts" in text
    assert "#52" not in text
