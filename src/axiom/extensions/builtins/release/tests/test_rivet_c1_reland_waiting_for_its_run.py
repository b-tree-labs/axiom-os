# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""RIVET C1 reland: a required check still waiting behind ``needs:`` is "waiting for its run", not "not run".

On #2436 itself the lander comment said "tests: not run. Unit Tests (3.13) never ran on head a3a93c7"
while that job was waiting behind ``needs: Smoke``. It started at 19:43:54, minutes after the report
(19:41:17), and passed. A required check with no row, while a CI workflow run for the head is queued or
in progress, is waiting. Only a completed run with no such check, or no run at all, is not run.

Acceptance ids:

  C1-RELAND-E  Recorded #2436 head a3a93c7 at the lander run's time: Unit Tests (3.13) has no row and
               the CI run is in progress: "waiting for its run", never listed under "tests: not run".
  C1-RELAND-F  The same head once the CI run has completed without that check, and a head with no
               run at all, are still "tests: not run".
  C1-RELAND-G  The ready-run case: a check that only has a draft run's row, while the ready run is in
               progress, is waiting too.
  C1-RELAND-H  The report says "waiting for its run" for it.

The data is recorded (``fixtures/rivet_c1/checkruns_head_a3a93c7.json`` and ``runs_head_a3a93c7.json``,
read with ``gh api``) and cut at the moment of the report: rows already started, and runs not yet
finished, as the lander saw them. The only reconstruction is each check's workflow name.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from axiom.extensions.builtins.release.lander.adapters import GitHubForge
from axiom.extensions.builtins.release.lander.config import LanderConfig
from axiom.extensions.builtins.release.lander.render import render_text
from axiom.extensions.builtins.release.lander.report import build_report
from axiom.extensions.builtins.release.selectors import Selection

FIX = Path(__file__).parent / "fixtures" / "rivet_c1"
HEAD = "a3a93c742de55e774342d611c4d6083315fce76c"
REPORTED = "2026-11-25T19:41:17Z"  # when lander run 45687563691 finished
REQUIRED = ("Guards", "Lint", "Unit Tests (3.13)")
REPO = "b-tree-labs/axiom-os-private"


def _at(moment: str):
    return datetime.fromisoformat(moment.replace("Z", "+00:00"))


def _rollup_at(moment: str) -> list[dict]:
    rows = []
    for run in json.loads((FIX / "checkruns_head_a3a93c7.json").read_text(encoding="utf-8"))["check_runs"]:
        if _at(run["started_at"]) > _at(moment):
            continue  # the check had not started: it has no row yet
        done = run["completed_at"] and _at(run["completed_at"]) <= _at(moment)
        rows.append(
            {
                "name": run["name"],
                "workflowName": "Loop guards" if run["name"] == "Guards" else "CI",
                "status": "COMPLETED" if done else "IN_PROGRESS",
                "conclusion": str(run["conclusion"]).upper() if done else "",
                "startedAt": run["started_at"],
                "completedAt": run["completed_at"] if done else None,
            }
        )
    return rows


def _runs_at(moment: str, *, finished: bool = False) -> list[dict]:
    rows = []
    for run in json.loads((FIX / "runs_head_a3a93c7.json").read_text(encoding="utf-8"))["workflow_runs"]:
        if _at(run["created_at"]) > _at(moment):
            continue
        over = finished or _at(run["updated_at"]) <= _at(moment)
        rows.append({**run, "status": "completed" if over else "in_progress", "conclusion": run["conclusion"] if over else None})
    return rows


def _forge(rollup: list[dict], runs: list[dict], *, now: str = REPORTED, draft: bool = False, ready: str | None = None) -> GitHubForge:
    def runner(args: list[str]) -> str:
        if args[:2] == ["pr", "list"]:
            return json.dumps(
                [{"number": 2436, "title": "RIVET C1", "headRefName": "feat/x", "headRefOid": HEAD, "baseRefName": "main",
                  "isDraft": draft, "mergeable": "MERGEABLE", "createdAt": "2026-11-25T18:00:00Z", "updatedAt": REPORTED}]
            )
        if args[:2] == ["pr", "view"]:
            return json.dumps({"files": [{"path": "src/a.py"}], "statusCheckRollup": rollup})
        if args[:2] == ["api", "graphql"]:
            nodes = [{"__typename": "ReadyForReviewEvent", "createdAt": ready}] if ready else []
            return json.dumps({"data": {"repository": {"pullRequest": {"timelineItems": {"nodes": nodes}}}}})
        path = args[-1]
        if path.endswith("/protection"):
            raise RuntimeError("404")
        if "/rules/branches/" in path:
            return json.dumps([{"type": "required_status_checks", "parameters": {"required_status_checks": [{"context": n} for n in REQUIRED]}}])
        if "/actions/runs?" in path:
            return json.dumps({"workflow_runs": runs})
        if "/commits/" in path:
            return json.dumps({"commit": {"committer": {"date": "2026-11-25T19:40:40Z"}}})
        raise AssertionError(f"unexpected call {args}")

    return GitHubForge(runner=runner, now=lambda: _at(now))


class _NoSelector:
    def select(self, *args, **kw):
        return Selection(True, (), "test")


def _report(pull):
    return build_report([pull], repo=REPO, config=LanderConfig(), tier_map=None, selector=_NoSelector())


# --- E ---------------------------------------------------------------------------------------------


def test_c1_reland_e_the_recorded_head_with_its_ci_run_in_progress_is_waiting_not_not_run():
    rollup = _rollup_at(REPORTED)
    assert "Unit Tests (3.13)" not in {r["name"] for r in rollup}, "the job had not started: no row"
    runs = _runs_at(REPORTED)
    assert any(r["name"] == "CI" and r["status"] == "in_progress" for r in runs)
    pull = _forge(rollup, runs).list_open(REPO)[0]
    assert pull.checks == "pending"
    assert pull.waiting_for_run == ("Unit Tests (3.13)",)
    assert pull.tests_not_run == ()


def test_c1_reland_e_the_report_does_not_list_it_under_tests_not_run():
    pull = _forge(_rollup_at(REPORTED), _runs_at(REPORTED)).list_open(REPO)[0]
    report = _report(pull)
    data = report.to_dict()
    assert data["tests_not_run"] == []
    assert "tests: not run" not in render_text(report)
    assert [v.number for v in report.waiting_on_ci] == [2436]


# --- H ---------------------------------------------------------------------------------------------


def test_c1_reland_h_the_report_says_waiting_for_its_run_and_names_the_check():
    pull = _forge(_rollup_at(REPORTED), _runs_at(REPORTED)).list_open(REPO)[0]
    report = _report(pull)
    row = report.to_dict()["waiting_for_run"][0]
    assert row["number"] == 2436 and row["checks"] == ["Unit Tests (3.13)"]
    assert row["sentence"].startswith("waiting for its run") and "Unit Tests (3.13)" in row["sentence"]
    assert "waiting for its run" in render_text(report)


# --- F ---------------------------------------------------------------------------------------------


def test_c1_reland_f_a_completed_ci_run_with_no_such_check_is_not_run():
    later = "2026-11-25T19:42:00Z"
    rollup = [r for r in _rollup_at("2026-11-25T20:30:00Z") if r["name"] != "Unit Tests (3.13)"]
    pull = _forge(rollup, _runs_at(later, finished=True), now=later).list_open(REPO)[0]
    assert pull.checks == "not_run" and pull.tests_not_run == ("Unit Tests (3.13)",)
    assert pull.waiting_for_run == ()


def test_c1_reland_f_no_run_at_all_is_not_run():
    pull = _forge(_rollup_at(REPORTED), []).list_open(REPO)[0]
    assert pull.checks == "not_run" and pull.tests_not_run == ("Unit Tests (3.13)",)


def test_c1_reland_f_the_landers_own_run_does_not_count_as_a_run_in_progress():
    own = [{"id": 1, "name": "RIVET lander", "path": ".github/workflows/rivet-lander.yml", "status": "in_progress", "conclusion": None}]
    pull = _forge(_rollup_at(REPORTED), own).list_open(REPO)[0]
    assert pull.checks == "not_run"


# --- G ---------------------------------------------------------------------------------------------


def test_c1_reland_g_a_check_with_only_a_draft_run_while_the_ready_run_is_in_progress_is_waiting():
    ready = "2026-11-25T19:40:00Z"
    moment = "2026-11-25T19:41:20Z"  # Guards and Lint have finished; Unit Tests has not started
    rollup = _rollup_at(moment)
    rollup.append({"name": "Unit Tests (3.13)", "workflowName": "CI", "status": "COMPLETED", "conclusion": "SUCCESS",
                   "startedAt": "2026-11-25T18:00:00Z", "completedAt": "2026-11-25T18:20:00Z"})
    pull = _forge(rollup, _runs_at(moment), now=moment, ready=ready).list_open(REPO)[0]
    assert pull.checks == "pending" and pull.waiting_for_run == ("Unit Tests (3.13)",)
