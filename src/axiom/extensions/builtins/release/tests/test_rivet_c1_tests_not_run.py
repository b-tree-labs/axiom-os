# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""RIVET C1: a draft's green is not evidence, and "no tests ran" is its own state (#2410, #2354).

Acceptance ids:

  C1-2410-A  A head whose required checks only ran while the pull request was a draft is not
             landable, whatever the checks report. The run that counts is one that started
             after the LAST ready-for-review event.
  C1-2410-B  A pull request that was never a draft lands exactly as it does today.
  C1-2410-C  Draft, ready, draft, ready: the run after the last ready event is the evidence.
  C1-2410-D  The forge adapter reads that event, and a read that fails is "unknown", not green.
  C1-2410-E  The act pass refuses to land such a pull request, with the reason in plain words.
  C1-2354-A  A required check nothing reported is the state "tests: not run", never "pending".
  C1-2354-B  The report lists it first, with the cause and the exact fix, and it is never ready.

The data is recorded: ``fixtures/rivet_c1`` holds the check rollups and ready-for-review
timelines of real pull requests (the 2026-11-24 incident, #2373, #2281), read with ``gh``.
"""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

from axiom.extensions.builtins.release.fixes import Diagnosis, Fix
from axiom.extensions.builtins.release.lander.actor import GitEffects, run_actor
from axiom.extensions.builtins.release.lander.adapters import GitHubForge, parse_pr
from axiom.extensions.builtins.release.lander.config import LanderConfig
from axiom.extensions.builtins.release.lander.model import OpenPull
from axiom.extensions.builtins.release.lander.render import render_text
from axiom.extensions.builtins.release.lander.report import build_report
from axiom.infra.orchestrator.approval import ApprovalGate
from axiom.infra.orchestrator.approval_store import InMemoryActionStore

FIX = Path(__file__).parent / "fixtures" / "rivet_c1"
REQUIRED = ("Guards", "Lint", "Unit Tests (3.13)")
REPO = "owner/name"
HEAD = "d" * 40
#: When #2360 was merged: the rollup RIVET could see at that second.
MERGED_2360 = "2026-11-24T19:34:49Z"


def _pr(number: int) -> dict:
    return json.loads((FIX / f"pr_{number}.json").read_text(encoding="utf-8"))


def _timeline(number: int) -> dict:
    return json.loads((FIX / f"timeline_{number}.json").read_text(encoding="utf-8"))


def _ready_at(number: int) -> str:
    nodes = _timeline(number)["data"]["repository"]["pullRequest"]["timelineItems"]["nodes"]
    return [n["createdAt"] for n in nodes if n["__typename"] == "ReadyForReviewEvent"][-1]


def _rollup_at(number: int, moment: str) -> list[dict]:
    """The rollup as it stood at ``moment``: only rows already started."""
    return [r for r in _pr(number)["statusCheckRollup"] if r["startedAt"] <= moment]


def _row(name: str, started: str, conclusion: str = "SUCCESS", workflow: str = "CI") -> dict:
    return {
        "name": name,
        "workflowName": workflow,
        "status": "COMPLETED",
        "conclusion": conclusion,
        "startedAt": started,
        "completedAt": started,
    }


def _parse(rollup: list[dict], *, ready: str | None, draft: bool = False, required=REQUIRED) -> OpenPull:
    row: dict = {"number": 7, "headRefOid": HEAD, "isDraft": draft, "statusCheckRollup": rollup}
    if ready is not None:
        row["readyForReviewAt"] = ready
    return parse_pr(row, required=tuple(required))


# --- C1-2410-A / B / C: the verdict --------------------------------------------------


def test_c1_2410_a_the_incident_head_at_merge_time_is_not_green():
    """2360 was merged 3 seconds after it was marked ready, on a draft run's green."""
    rollup = _rollup_at(2360, MERGED_2360)
    names = {r["name"] for r in rollup}
    assert set(REQUIRED) <= names, "the recorded rollup carries the three required checks, all green"
    pull = _parse(rollup, ready=_ready_at(2360))
    assert pull.checks == "not_run"
    assert pull.tests_not_run == REQUIRED
    assert pull.tests_not_run_cause == "draft_run_only"


def test_c1_2410_a_the_same_head_is_green_once_the_ready_run_has_finished():
    pull = _parse(_pr(2360)["statusCheckRollup"], ready=_ready_at(2360))
    assert pull.checks == "passing"
    assert pull.tests_not_run == ()


def test_c1_2410_a_one_required_check_still_on_its_draft_run_keeps_it_held():
    ready = "2026-11-24T19:00:00Z"
    rollup = [
        _row("Guards", "2026-11-24T19:00:05Z"),
        _row("Lint", "2026-11-24T19:00:05Z"),
        _row("Unit Tests (3.13)", "2026-11-24T18:00:00Z"),  # the draft run's skip-green
    ]
    pull = _parse(rollup, ready=ready)
    assert pull.checks == "not_run"
    assert pull.tests_not_run == ("Unit Tests (3.13)",)


def test_c1_2410_b_a_pull_request_that_was_never_a_draft_lands_as_today():
    rollup = _rollup_at(2360, MERGED_2360)
    assert _parse(rollup, ready=None).checks == "passing"
    assert parse_pr({"number": 7, "statusCheckRollup": rollup}, required=REQUIRED).checks == "passing"


def test_c1_2410_c_draft_ready_draft_ready_uses_the_run_after_the_last_ready_event():
    first_ready, second_ready = "2026-11-24T10:00:00Z", "2026-11-24T12:00:00Z"
    ran_after_first = [_row(n, "2026-11-24T10:05:00Z") for n in REQUIRED]
    assert _parse(ran_after_first, ready=first_ready).checks == "passing"
    assert _parse(ran_after_first, ready=second_ready).checks == "not_run"
    ran_after_second = ran_after_first + [_row(n, "2026-11-24T12:05:00Z") for n in REQUIRED]
    assert _parse(ran_after_second, ready=second_ready).checks == "passing"


def test_c1_2410_a_a_failing_required_check_stays_failing_not_not_run():
    rollup = [_row("Guards", "2026-11-24T19:05:00Z", "FAILURE"), _row("Lint", "2026-11-24T19:05:00Z")]
    pull = _parse(rollup, ready="2026-11-24T19:00:00Z")
    assert pull.checks == "failing"


def test_c1_2410_a_a_draft_is_not_run_whatever_its_checks_say():
    pull = _parse(_pr(2360)["statusCheckRollup"], ready=None, draft=True)
    assert pull.checks == "not_run"
    assert pull.tests_not_run_cause == "draft"


# --- C1-2410-D: the forge adapter -----------------------------------------------------


def _forge(rollup: list[dict], *, graphql=None, draft: bool = False) -> GitHubForge:
    def runner(args: list[str]) -> str:
        if args[:2] == ["pr", "list"]:
            return json.dumps(
                [
                    {
                        "number": 7,
                        "title": "change",
                        "headRefName": "topic/one",
                        "headRefOid": HEAD,
                        "baseRefName": "main",
                        "isDraft": draft,
                        "mergeable": "MERGEABLE",
                        "createdAt": "2026-11-24T08:00:00Z",
                        "updatedAt": "2026-11-24T19:34:49Z",
                    }
                ]
            )
        if args[:2] == ["pr", "view"]:
            return json.dumps({"files": [{"path": "migrations/a.sql"}], "statusCheckRollup": rollup})
        if args[:2] == ["api", "graphql"]:
            if graphql is None:
                raise AssertionError("no timeline was expected")
            if isinstance(graphql, Exception):
                raise graphql
            return json.dumps(graphql)
        path = args[-1]
        if path.endswith("/protection"):
            raise RuntimeError("404")
        if "/rules/branches/" in path:
            return json.dumps(
                [
                    {
                        "type": "required_status_checks",
                        "parameters": {"required_status_checks": [{"context": n} for n in REQUIRED]},
                    }
                ]
            )
        raise AssertionError(f"unexpected call {args}")

    return GitHubForge(runner=runner, now=lambda: datetime(2026, 11, 24, 19, 34, 49, tzinfo=UTC))


def test_c1_2410_d_the_forge_reads_the_ready_event_and_holds_the_incident_head():
    forge = _forge(_rollup_at(2360, MERGED_2360), graphql=_timeline(2360))
    pull = forge.list_open(REPO)[0]
    assert pull.checks == "not_run"
    assert pull.tests_not_run_cause == "draft_run_only"
    assert pull.ready_at == datetime(2026, 11, 24, 19, 34, 46, tzinfo=UTC)


def test_c1_2410_d_the_last_of_several_ready_events_counts():
    nodes = [
        {"__typename": "ReadyForReviewEvent", "createdAt": "2026-11-24T10:00:00Z"},
        {"__typename": "ReadyForReviewEvent", "createdAt": "2026-11-24T12:00:00Z"},
    ]
    answer = {"data": {"repository": {"pullRequest": {"timelineItems": {"nodes": nodes}}}}}
    rollup = [_row(n, "2026-11-24T10:05:00Z") for n in REQUIRED]
    pull = _forge(rollup, graphql=answer).list_open(REPO)[0]
    assert pull.checks == "not_run"


def test_c1_2410_d_a_timeline_that_cannot_be_read_is_unknown_not_green():
    rollup = [_row(n, "2026-11-24T10:05:00Z") for n in REQUIRED]
    pull = _forge(rollup, graphql=RuntimeError("502")).list_open(REPO)[0]
    assert pull.checks == "unknown"


def test_c1_2410_d_a_head_with_failing_checks_costs_no_timeline_read():
    rollup = [_row("Guards", "2026-11-24T10:05:00Z", "FAILURE")]
    pull = _forge(rollup).list_open(REPO)[0]  # graphql raises if it is asked
    assert pull.checks == "failing"


# --- C1-2410-E: the act pass ---------------------------------------------------------


class _Effects:
    def __init__(self) -> None:
        self.landed: list[int] = []

    def head(self, number: int) -> str:
        return HEAD

    def apply(self, number, fix, head_sha):  # pragma: no cover
        raise AssertionError("nothing is applied")

    def rerun(self, number, sha):  # pragma: no cover
        return "passing"

    def land(self, number, sha, method):
        self.landed.append(number)

    def close(self, number, sha):  # pragma: no cover
        raise AssertionError("nothing is closed")

    def add_label(self, number, label):
        pass

    def remove_label(self, number, label):
        pass


class _Fixes:
    name = "fake"

    def diagnose(self, pull: OpenPull) -> Diagnosis:
        return Diagnosis("clear", "green", pull.files) if pull.checks == "passing" else Diagnosis("unknown", "x", pull.files)

    def draft(self, diagnosis, pull) -> Fix:
        return Fix(summary="land", kind="land") if diagnosis.kind == "clear" else Fix(summary="hold", needs_human=True, reason="no fixer configured")


def _config() -> LanderConfig:
    return replace(LanderConfig(), approvers=("@ada:local",), auto_allowed=("merge",))


def _not_run_pull(**kw) -> OpenPull:
    base = dict(
        number=7, title="t", head_sha=HEAD, head_ref="b", files=("src/a.py",), checks="not_run",
        mergeable=True, tests_not_run=REQUIRED, tests_not_run_cause="draft_run_only",
    )
    base.update(kw)
    return OpenPull(**base)


def test_c1_2410_e_the_act_pass_does_not_land_a_pull_request_whose_tests_have_not_run(tmp_path):
    effects = _Effects()
    result = run_actor(
        [_not_run_pull()], repo=REPO, config=_config(), state_dir=tmp_path, effects=effects,
        gate=ApprovalGate(InMemoryActionStore()), provider=_Fixes(), environ={},
    )
    assert effects.landed == []
    outcome = result.outcomes[0]
    assert outcome.status in {"skipped", "escalated", "held"}
    assert "tests not run" in outcome.reason
    assert "Guards" in outcome.reason


def test_c1_2410_e_the_last_gate_before_the_merge_re_checks_the_ready_run():
    """GitEffects.rerun is the pinned check just before the merge button."""
    rollup = _rollup_at(2360, MERGED_2360)

    class Fake(GitEffects):
        def _run(self, args, *, token_env=""):
            if args[:3] == ["gh", "pr", "view"]:
                return json.dumps({"headRefOid": HEAD, "baseRefName": "main", "isDraft": False, "statusCheckRollup": rollup})
            if args[:3] == ["gh", "api", "graphql"]:
                return json.dumps(_timeline(2360))
            if "/rules/branches/" in args[-1]:
                return json.dumps(
                    [{"type": "required_status_checks", "parameters": {"required_status_checks": [{"context": n} for n in REQUIRED]}}]
                )
            raise RuntimeError("404")

    assert Fake(repo=REPO, token_env="GH_TOKEN").rerun(7, HEAD) != "passing"


# --- C1-2354-A / B: tests: not run ----------------------------------------------------


def test_c1_2354_a_281_had_run_nothing_but_the_landers_own_job():
    """The recorded #2281 rollup, cut to what it had at the time: only RIVET's own job."""
    only_land = [r for r in _pr(2281)["statusCheckRollup"] if r["name"] == "land"]
    assert len(only_land) == 1
    pull = _parse(only_land, ready=None)
    assert pull.checks == "not_run"
    assert pull.tests_not_run == REQUIRED
    assert pull.tests_not_run_cause == "never_reported"


def test_c1_2354_a_the_head_281_has_today_is_green_and_is_not_called_not_run():
    pull = _parse(_pr(2281)["statusCheckRollup"], ready=_ready_at(2281))
    assert pull.checks == "passing"


def test_c1_2354_a_a_normal_green_pull_request_is_untouched():
    pull = _parse(_pr(2373)["statusCheckRollup"], ready=_ready_at(2373))
    assert pull.checks == "passing"


def _report(*pulls: OpenPull, **kw):
    return build_report(list(pulls), repo=REPO, config=_config(), tier_map=None, selector=_NoSelector(), **kw)


class _NoSelector:
    def select(self, *args, **kw):
        from axiom.extensions.builtins.release.selectors import Selection

        return Selection(True, (), "test")


def test_c1_2354_b_a_stuck_head_is_listed_first_with_its_cause_and_the_exact_fix():
    pull = _not_run_pull(
        tests_not_run_cause="never_reported", ci_never_ran=True, missing_checks=REQUIRED,
        head_sha="a" * 40,
    )
    report = _report(pull)
    data = report.to_dict()
    row = data["tests_not_run"][0]
    assert row["number"] == 7 and row["checks"] == list(REQUIRED) and row["cause"] == "never_reported"
    assert row["sentence"].startswith("tests: not run")
    assert "never ran on head aaaaaaa" in row["sentence"]
    assert "gh pr close 7 && gh pr reopen 7" in row["fix"]
    assert data["ready"] == []
    text = render_text(report)
    lines = text.splitlines()
    first = next(i for i, line in enumerate(lines) if line.startswith("tests: not run"))
    assert first < next(i for i, line in enumerate(lines) if line.startswith("ready to land"))
    assert "Guards" in text and "gh pr close 7" in text


def test_c1_2354_b_a_draft_run_is_named_for_what_it_is():
    row = _report(_not_run_pull(ci_never_ran=True)).to_dict()["tests_not_run"][0]
    assert "draft" in row["sentence"] and "marked ready" in row["fix"]


def test_c1_2354_b_a_head_still_inside_the_grace_period_waits_and_is_still_not_ready():
    report = _report(_not_run_pull(tests_not_run_cause="never_reported"))
    assert [v.number for v in report.waiting_on_ci] == [7]
    assert report.ready == [] and [r["number"] for r in report.to_dict()["tests_not_run"]] == [7]


def test_c1_2354_b_a_draft_goes_to_its_author_with_the_tests_not_run_reason():
    report = _report(_not_run_pull(draft=True, tests_not_run_cause="draft"))
    assert [v.number for v in report.with_author] == [7]
    assert "tests_not_run:draft" in report.with_author[0].reasons


def test_c1_2354_b_a_green_pull_request_has_no_tests_not_run_section():
    green = OpenPull(number=8, title="t", head_sha=HEAD, head_ref="b", files=("src/a.py",), checks="passing", mergeable=True)
    report = _report(green)
    assert report.to_dict()["tests_not_run"] == []
    assert "tests: not run" not in render_text(report)
    assert [v.number for v in report.ready] == [8]


def test_c1_2354_b_the_report_verdict_names_the_missing_run_not_a_conftest():
    from axiom.extensions.builtins.release.lander import verdict

    pull = _not_run_pull(draft=True, tests_not_run_cause="draft", files=("conftest.py",))
    data = _report(pull).to_dict()
    found = verdict.verdict_for(data, 7)
    assert found is not None
    assert "tests: not run" in " ".join(found.why)
    summary = verdict.summary_markdown(data)
    assert summary.index("tests: not run") < summary.index("Ready to land")


def test_c1_2354_b_a_head_with_no_named_checks_says_no_check_reported_and_long_lists_are_cut():
    row = _report(_not_run_pull(tests_not_run=(), tests_not_run_cause="never_reported", ci_never_ran=True, missing_checks=())).to_dict()["tests_not_run"][0]
    assert row["sentence"].startswith("tests: not run. No check has reported on head")
    many = tuple(f"job {i}" for i in range(9))
    long = _report(_not_run_pull(tests_not_run=many, draft=True, tests_not_run_cause="draft")).to_dict()["tests_not_run"][0]
    assert "and 4 more" in long["sentence"] and "job 8" not in long["sentence"]


def test_c1_2354_b_the_row_says_who_acts_or_that_nobody_does_yet():
    waiting = _report(_not_run_pull(tests_not_run_cause="never_reported")).to_dict()["tests_not_run"][0]
    overdue = _report(_not_run_pull(tests_not_run_cause="never_reported", ci_never_ran=True, missing_checks=REQUIRED)).to_dict()["tests_not_run"][0]
    draft = _report(_not_run_pull(draft=True, tests_not_run_cause="draft")).to_dict()["tests_not_run"][0]
    assert (waiting["status"], overdue["status"], draft["status"]) == ("waiting", "overdue", "draft")


# --- a path-filtered skip on a ready pull request is not "not run" -------------------------------

#: Every job of ci.yml plus the loop guards, by the names a recent green run on main reported.
WIDE_REQUIRED = (
    "Build Wheel", "Container Smoke (stranger path)", "Guards", "Install-Path Tests",
    "Integration Tests", "Lint", "Migration Tests", "Smoke", "Unit Tests (3.13)",
)


def test_c1_required_a_docs_only_ready_pull_request_still_lands_with_the_wide_required_set():
    """#2426 changed docs only: its ready run skipped Integration, Install-Path, Migration and Build
    Wheel by path filter. A skip that ran after the ready event is a pass, so it must still land."""
    pull = _parse(_pr(2426)["statusCheckRollup"], ready=_ready_at(2426), required=WIDE_REQUIRED)
    assert pull.checks == "passing", (pull.checks, pull.tests_not_run)


def test_c1_required_a_skip_that_only_happened_on_the_draft_run_is_not_a_pass():
    rollup = _rollup_at(2360, MERGED_2360)
    pull = _parse(rollup, ready=_ready_at(2360), required=("Migration Tests", "Lint"))
    assert pull.checks == "not_run" and "Migration Tests" in pull.tests_not_run
