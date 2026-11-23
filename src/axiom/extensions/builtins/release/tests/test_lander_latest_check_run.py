# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""RIVET judges each check by its latest run on the head, not by every run it ever had.

Marking a draft ready, or repeating a run, starts a new run of the same workflow on the
same commit and cancels the first. The rollup then holds both: ``Lint`` CANCELLED at
06:34:10 and ``Lint`` SUCCESS at 06:34:34. The forge calls that pull request CLEAN, and
RIVET called it failing ("no fixer configured") and never landed it. Every draft marked
ready met this.
"""

from __future__ import annotations

from axiom.extensions.builtins.release.lander.adapters import (
    checks_from_rollup,
    failing_checks_from_rollup,
)
from axiom.extensions.builtins.release.lander.checks import FAILURE, PENDING, SUCCESS, check_verdict


def _run(name: str, conclusion: str, at: str, workflow: str = "CI", status: str = "COMPLETED") -> dict:
    row = {"name": name, "workflowName": workflow, "status": status, "conclusion": conclusion}
    if status == "COMPLETED":
        row["completedAt"] = at
    else:
        row["startedAt"] = at
        row["completedAt"] = "0001-01-01T00:00:00Z"
    return row


OLD_CANCELLED_NEW_OK = [
    _run("Lint", "CANCELLED", "2026-11-22T06:34:10Z"),
    _run("Lint", "SUCCESS", "2026-11-22T06:34:34Z"),
]


def test_lr1_a_superseded_cancelled_run_does_not_make_the_check_fail():
    assert checks_from_rollup(OLD_CANCELLED_NEW_OK) == "passing"
    assert failing_checks_from_rollup(OLD_CANCELLED_NEW_OK) == ()
    assert check_verdict(OLD_CANCELLED_NEW_OK, ()).state == SUCCESS


def test_lr2_the_order_in_the_list_does_not_matter():
    reversed_rows = list(reversed(OLD_CANCELLED_NEW_OK))
    assert checks_from_rollup(reversed_rows) == "passing"
    assert check_verdict(reversed_rows, ()).state == SUCCESS


def test_lr3_a_newer_failure_still_fails():
    rows = [_run("Lint", "SUCCESS", "2026-11-22T06:00:00Z"), _run("Lint", "FAILURE", "2026-11-22T07:00:00Z")]
    assert checks_from_rollup(rows) == "failing"
    assert [c.name for c in failing_checks_from_rollup(rows)] == ["Lint"]
    assert check_verdict(rows, ()).state == FAILURE


def test_lr4_a_run_still_going_is_the_newest():
    rows = [
        _run("Lint", "CANCELLED", "2026-11-22T06:34:10Z"),
        _run("Lint", "", "2026-11-22T06:34:20Z", status="IN_PROGRESS"),
    ]
    assert checks_from_rollup(rows) == "pending"
    assert check_verdict(rows, ()).state == PENDING


def test_lr5_the_same_check_name_in_two_workflows_is_two_checks():
    rows = [
        _run("Guards", "SUCCESS", "2026-11-22T06:34:34Z", workflow="CI"),
        _run("Guards", "CANCELLED", "2026-11-22T06:34:40Z", workflow="Loop guards"),
    ]
    assert checks_from_rollup(rows) == "failing"
    assert [c.name for c in failing_checks_from_rollup(rows)] == ["Guards"]


def test_lr6_a_required_check_is_judged_by_its_latest_run():
    assert check_verdict(OLD_CANCELLED_NEW_OK, ("Lint",)).state == SUCCESS
    old_ok_new_bad = [_run("Lint", "SUCCESS", "2026-11-22T06:00:00Z"), _run("Lint", "CANCELLED", "2026-11-22T07:00:00Z")]
    verdict = check_verdict(old_ok_new_bad, ("Lint",))
    assert verdict.state == FAILURE and verdict.failing == ("Lint",)
