# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""A head labelled ``ci:full`` lands only after a full run on that head went green (#2441).

Recorded data, read with ``gh api`` on 2026-11-26 (read only):

* ``data/pr_rollup_narrowed_2488.json``: ``gh pr view 2488 --json statusCheckRollup,labels``.
  A narrowed pull-request run: the shard job reports one SKIPPED row named ``Unit Shard``.
* ``data/main_check_runs_full_run.json``: the check runs of main's full run on
  d47b2dba (``Unit Shard (1)``, ``(2)``, ``(3)``, SUCCESS). A pull request that takes the main
  path reports the same names.
"""

from __future__ import annotations

import json
from pathlib import Path

from axiom.extensions.builtins.release.lander.checks import (
    CAUSE_FULL_RUN_MISSING,
    FULL_LABEL,
    NOT_RUN,
    PENDING,
    SUCCESS,
    check_verdict,
    not_run_fix,
    not_run_sentence,
)

DATA = Path(__file__).parent / "data"
REQUIRED = ("Lint", "Unit Tests (3.13)", "Guards")


def _narrowed() -> list[dict]:
    return json.loads((DATA / "pr_rollup_narrowed_2488.json").read_text())["statusCheckRollup"]


def _full_rows() -> list[dict]:
    """main's recorded check runs, in the shape ``gh pr view`` gives a rollup."""
    rows = []
    for run in json.loads((DATA / "main_check_runs_full_run.json").read_text()):
        rows.append(
            {
                "__typename": "CheckRun",
                "workflowName": "CI",
                "name": run["name"],
                "status": run["status"].upper(),
                "conclusion": run["conclusion"].upper(),
                "startedAt": run["started_at"],
                "completedAt": run["completed_at"],
            }
        )
    return rows


def _on_a_pull_request(rows: list[dict]) -> list[dict]:
    """Narrowed rows with the required checks main's run does not carry."""
    keep = {r["name"] for r in rows}
    base = [r for r in _narrowed() if r["name"] not in keep]
    return base + rows + [
        {"workflowName": "Loop guards", "name": "Guards", "status": "COMPLETED", "conclusion": "SUCCESS"}
    ]


def test_the_recorded_narrowed_run_has_one_skipped_shard_row():
    shard = [r for r in _narrowed() if str(r["name"]).startswith("Unit Shard")]
    assert [(r["name"], r["conclusion"]) for r in shard] == [("Unit Shard", "SKIPPED")]


def test_without_the_label_a_narrowed_run_is_judged_as_before():
    verdict = check_verdict(_narrowed(), REQUIRED)
    assert verdict.not_run == ()


def test_the_label_with_only_a_narrowed_run_reads_as_its_own_state():
    verdict = check_verdict(_narrowed(), REQUIRED, labels=[FULL_LABEL])
    assert verdict.state == PENDING
    assert verdict.not_run_cause == CAUSE_FULL_RUN_MISSING
    assert verdict.not_run == ("Unit Shard",)
    assert verdict.reason().startswith("tests: not run.")


def test_the_label_matches_without_regard_to_case():
    verdict = check_verdict(_narrowed(), REQUIRED, labels=["CI:Full"])
    assert verdict.not_run_cause == CAUSE_FULL_RUN_MISSING


def test_the_label_with_a_green_full_run_on_the_head_is_success():
    rows = _on_a_pull_request(_full_rows())
    verdict = check_verdict(rows, REQUIRED, labels=[FULL_LABEL])
    assert verdict.state == SUCCESS and not verdict.not_run


def test_a_red_shard_is_a_failure_naming_the_shard():
    rows = _on_a_pull_request(_full_rows())
    for row in rows:
        if row["name"] == "Unit Shard (2)":
            row["conclusion"] = "FAILURE"
    verdict = check_verdict(rows, REQUIRED, labels=[FULL_LABEL])
    assert verdict.state == "failure"
    assert "Unit Shard (2)" in verdict.failing


def test_a_shard_still_running_is_pending_not_not_run():
    rows = _on_a_pull_request(_full_rows())
    for row in rows:
        if row["name"] == "Unit Shard (3)":
            row["status"], row["conclusion"] = "IN_PROGRESS", ""
    verdict = check_verdict(rows, REQUIRED, labels=[FULL_LABEL])
    assert verdict.state == PENDING and not verdict.not_run
    assert "Unit Shard (3)" in verdict.pending


def test_an_unlabelled_pull_request_does_not_need_shards():
    assert check_verdict(_on_a_pull_request(_full_rows()), REQUIRED).state == SUCCESS


def test_the_hold_names_the_exact_fix():
    fix = not_run_fix(CAUSE_FULL_RUN_MISSING, 77)
    assert "gh pr edit 77 --remove-label ci:full" in fix and "--add-label ci:full" in fix
    sentence = not_run_sentence(("Unit Shard",), CAUSE_FULL_RUN_MISSING, head="abcdef1234")
    assert "ci:full" in sentence and "abcdef1" in sentence


def test_the_not_run_state_reaches_the_pull_request():
    from axiom.extensions.builtins.release.lander.adapters import parse_pr

    row = {
        "number": 77,
        "title": "t",
        "headRefName": "x",
        "headRefOid": "a" * 40,
        "baseRefName": "main",
        "author": {"login": "someone"},
        "labels": [{"name": FULL_LABEL}],
        "statusCheckRollup": _narrowed(),
        "isDraft": False,
        "mergeable": "MERGEABLE",
        "files": [],
    }
    pull = parse_pr(row, required=REQUIRED)
    assert pull.checks == NOT_RUN
    assert pull.tests_not_run_cause == CAUSE_FULL_RUN_MISSING
    row["labels"] = []
    assert parse_pr(row, required=REQUIRED).checks != NOT_RUN
