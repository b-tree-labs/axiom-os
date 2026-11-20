# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""RIVET judges a pull request by the checks its base branch requires (#2147).

On 2026-11-19 a pull request had every required check green and was held as
"failing" because RIVET's own earlier report job was red, and a non-required
job's failure counted the same way.
"""

from __future__ import annotations

import json

from axiom.extensions.builtins.release.lander.adapters import GitHubForge, is_self_check, parse_pr


def _ok(name: str) -> dict:
    return {"name": name, "status": "COMPLETED", "conclusion": "SUCCESS"}


def _bad(name: str, conclusion: str = "FAILURE", workflow: str = "") -> dict:
    row = {"name": name, "status": "COMPLETED", "conclusion": conclusion}
    if workflow:
        row["workflowName"] = workflow
    return row


def _forge(rollup: list[dict], required: list[str] | None) -> GitHubForge:
    def runner(args: list[str]) -> str:
        if args[0:2] == ["pr", "list"]:
            return json.dumps(
                [
                    {
                        "number": 7,
                        "title": "t",
                        "headRefName": "b",
                        "headRefOid": "abc1234",
                        "baseRefName": "main",
                        "author": {"login": "a"},
                        "mergeable": "MERGEABLE",
                    }
                ]
            )
        if args[0] == "api" and args[1].endswith("/protection"):
            if required is None:
                raise RuntimeError("404 Branch not protected")
            return json.dumps({"required_status_checks": {"contexts": required}})
        if args[0] == "api" and "/rules/branches/" in args[1]:
            return "[]"
        return json.dumps({"files": [{"path": "src/a.py"}], "statusCheckRollup": rollup})

    return GitHubForge(runner=runner)


def test_rivets_own_red_check_does_not_hold_the_pull_request():
    rollup = [_ok("test"), _bad("Rivet lander (warn-only)")]
    assert parse_pr({"number": 1, "statusCheckRollup": rollup}).checks == "passing"
    pull = _forge(rollup, None).list_open("acme/w")[0]
    assert pull.checks == "passing"
    assert pull.failing_checks == ()


def test_self_check_is_recognised_by_job_or_workflow_name():
    assert is_self_check({"name": "Rivet command"})
    assert is_self_check({"name": "land", "workflowName": "RIVET lander"})
    assert not is_self_check({"name": "Backend unit tests", "workflowName": "Gate"})
    assert not is_self_check({"name": "Riveting tests"})


def test_only_required_checks_decide_when_the_branch_names_them():
    rollup = [_ok("test"), _bad("nightly-extra")]
    pull = _forge(rollup, ["test"]).list_open("acme/w")[0]
    assert pull.checks == "passing"
    assert pull.failing_checks == ()


def test_a_failing_required_check_still_fails():
    rollup = [_bad("test"), _ok("lint")]
    pull = _forge(rollup, ["test", "lint"]).list_open("acme/w")[0]
    assert pull.checks == "failing"
    assert [check.name for check in pull.failing_checks] == ["test"]


def test_a_required_check_that_has_not_reported_is_pending():
    pull = _forge([_ok("lint")], ["test", "lint"]).list_open("acme/w")[0]
    assert pull.checks == "pending"


def test_an_unprotected_branch_falls_back_to_every_check_but_rivets_own():
    rollup = [_ok("test"), _bad("lint"), _bad("Rivet contract")]
    pull = _forge(rollup, None).list_open("acme/w")[0]
    assert pull.checks == "failing"
    assert [check.name for check in pull.failing_checks] == ["lint"]
