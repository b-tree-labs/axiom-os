# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""The backlog report lists under "needs a person" only what a person must decide (ADR-269).

A failing check RIVET is still repairing, CI it cannot read yet, and an overlap it lands one
at a time are RIVET's work; a draft is the author's. Listing them as needing a person sent
people to pull requests nobody had to look at.
"""

from __future__ import annotations

from axiom.extensions.builtins.release.lander.render import render_text
from axiom.extensions.builtins.release.lander.report import BacklogReport, PullView, _owner


def test_reasons_rivet_works_through_are_rivets():
    assert _owner(("failing_checks",)) == "rivet"
    assert _owner(("checks_unknown",)) == "rivet"
    assert _owner(("file_overlap:4,5", "failing_checks")) == "rivet"


def test_a_draft_is_the_authors():
    assert _owner(("draft",)) == "author"
    assert _owner(("draft", "failing_checks")) == "author"


def test_anything_a_person_decides_wins():
    for reason in ("try-cap", "security", "conflict", "approval_reset", "review_refused"):
        assert _owner((reason, "failing_checks", "draft")) == "person"


def test_an_unknown_reason_is_held_for_a_person():
    assert _owner(("something_new",)) == "person"
    assert _owner(()) == "person"


def test_the_text_report_names_each_group():
    def view(number, reasons):
        return PullView(number=number, title=f"t{number}", head_sha="s", head_ref="b", checks="failing",
                        approval="none", reasons=reasons)

    report = BacklogReport(
        repo="acme/widgets", mode="warn", suite_minutes=10, suite_minutes_source="default",
        needs_human=[view(1, ("try-cap",))],
        rivet_handling=[view(2, ("failing_checks",))],
        with_author=[view(3, ("draft",))],
    )
    text = render_text(report)
    assert "needs a person (1):\n  #1 try-cap" in text
    assert "RIVET is handling (1):\n  #2 failing_checks" in text
    assert "with the author (1):\n  #3 draft" in text
    assert [n for n in ("rivet_handling", "with_author") if n in report.to_dict()] == [
        "rivet_handling", "with_author"]
