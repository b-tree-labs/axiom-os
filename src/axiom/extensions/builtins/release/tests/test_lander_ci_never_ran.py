# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""CI that never ran on a head is not "still running", and a claim is not a sweep.

Three things kept pull requests stuck with nobody acting: required checks that
no workflow run would ever report were read as pending; nothing started them;
and every acting sweep labelled and unlabelled every open pull request, which
moved each one's update time and hid the ones that really changed.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from axiom.extensions.builtins.release.fixes import Diagnosis, Fix
from axiom.extensions.builtins.release.lander import ci_start
from axiom.extensions.builtins.release.lander.actor import close_command, reopen_command, run_actor
from axiom.extensions.builtins.release.lander.adapters import GitHubForge
from axiom.extensions.builtins.release.lander.checks import (
    CI_NEVER_RAN,
    CI_NEVER_RAN_PERSON,
    active_runs,
    check_verdict,
    ci_never_ran,
)
from axiom.extensions.builtins.release.lander.config import LanderConfig, parse_config
from axiom.extensions.builtins.release.lander.loop_guard import LanderRefused
from axiom.extensions.builtins.release.lander.model import OpenPull
from axiom.extensions.builtins.release.lander.render import render_text
from axiom.extensions.builtins.release.lander.report import build_report
from axiom.infra.orchestrator.approval import ApprovalGate
from axiom.infra.orchestrator.approval_store import InMemoryActionStore

REQUIRED = ("Guards", "Lint", "Unit Tests (3.13)")
GRACE = 1800.0
NOW = datetime(2026, 1, 10, 12, 0, tzinfo=UTC)
REPO = "owner/name"
HEAD = "a" * 40


# --- verdict ---------------------------------------------------------------


def test_none_reported_no_active_run_and_older_than_grace_is_ci_never_ran():
    verdict = check_verdict([], REQUIRED)
    assert verdict.missing == REQUIRED
    assert ci_never_ran(verdict, active_runs=0, head_age_s=GRACE + 1, grace_s=GRACE)


def test_a_queued_or_running_run_keeps_it_pending():
    verdict = check_verdict([], REQUIRED)
    assert active_runs([{"name": "CI", "status": "queued"}]) == 1
    assert active_runs([{"name": "CI", "status": "in_progress"}]) == 1
    assert not ci_never_ran(verdict, active_runs=1, head_age_s=GRACE + 1, grace_s=GRACE)


def test_a_head_younger_than_the_grace_period_keeps_it_pending():
    verdict = check_verdict([], REQUIRED)
    assert not ci_never_ran(verdict, active_runs=0, head_age_s=GRACE - 1, grace_s=GRACE)


def test_some_checks_reported_is_not_this_reason():
    verdict = check_verdict([{"name": "Lint", "status": "COMPLETED", "conclusion": "SUCCESS"}], REQUIRED)
    assert verdict.missing == ("Guards", "Unit Tests (3.13)")
    assert not ci_never_ran(verdict, active_runs=0, head_age_s=GRACE + 1, grace_s=GRACE)


def test_facts_that_could_not_be_read_keep_it_pending():
    verdict = check_verdict([], REQUIRED)
    assert not ci_never_ran(verdict, active_runs=None, head_age_s=GRACE + 1, grace_s=GRACE)
    assert not ci_never_ran(verdict, active_runs=0, head_age_s=None, grace_s=GRACE)


def test_the_landers_own_run_and_finished_runs_are_not_active():
    rows = [
        {"name": "RIVET lander", "status": "in_progress"},
        {"name": "CI", "status": "completed"},
    ]
    assert active_runs(rows) == 0
    assert active_runs("not a list") is None


# --- the forge gathers the facts ------------------------------------------


def _runner(*, rollup, runs, pushed, reopened=(), draft=False, fork=False, auto_merge=False):
    calls: list[list[str]] = []

    def run(args: list[str]) -> str:
        calls.append(args)
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
                        "isCrossRepository": fork,
                        "autoMergeRequest": {"enabledAt": "x"} if auto_merge else None,
                        "createdAt": (NOW - timedelta(days=9)).isoformat(),
                        "updatedAt": NOW.isoformat(),
                    }
                ]
            )
        if args[:2] == ["pr", "view"]:
            return json.dumps({"files": [{"path": "src/a.py"}], "statusCheckRollup": rollup})
        if args[0] == "api" and args[1] == "graphql":
            nodes = [{"createdAt": moment.isoformat()} for moment in reopened]
            return json.dumps(
                {"data": {"repository": {"pullRequest": {"timelineItems": {"nodes": nodes}}}}}
            )
        path = args[-1]
        if path.endswith("/protection"):
            raise RuntimeError("404")
        if "/rules/branches/" in path:
            return json.dumps(
                [
                    {
                        "type": "required_status_checks",
                        "parameters": {"required_status_checks": [{"context": name} for name in REQUIRED]},
                    }
                ]
            )
        if "/actions/runs?" in path:
            assert f"head_sha={HEAD}" in path
            return json.dumps({"workflow_runs": runs})
        if "/commits/" in path:
            return json.dumps({"commit": {"committer": {"date": pushed.isoformat()}}})
        raise AssertionError(f"unexpected call {args}")

    return run, calls


def _listed(**kwargs) -> OpenPull:
    run, _ = _runner(**kwargs)
    return GitHubForge(run, ci_grace_s=GRACE, now=lambda: NOW).list_open(REPO)[0]


def test_the_forge_marks_a_head_no_workflow_ever_ran_on():
    pull = _listed(rollup=[], runs=[], pushed=NOW - timedelta(days=7))
    assert pull.ci_never_ran is True
    assert pull.missing_checks == REQUIRED
    assert pull.ci_start_spent is False


def test_only_the_landers_own_run_on_the_head_is_still_never_ran():
    own = [{"name": "RIVET lander", "status": "in_progress", "event": "pull_request"}]
    rollup = [{"name": "land", "workflowName": "RIVET lander", "status": "IN_PROGRESS"}]
    pull = _listed(rollup=rollup, runs=own, pushed=NOW - timedelta(days=3))
    assert pull.ci_never_ran is True


def test_the_forge_leaves_a_queued_run_and_a_fresh_head_pending():
    queued = _listed(rollup=[], runs=[{"name": "CI", "status": "queued"}], pushed=NOW - timedelta(days=7))
    assert queued.ci_never_ran is False
    assert queued.checks == "pending"
    fresh = _listed(rollup=[], runs=[], pushed=NOW - timedelta(minutes=5))
    assert fresh.ci_never_ran is False


def test_the_forge_reads_a_reopen_after_the_head_as_the_one_start_already_spent():
    pushed = NOW - timedelta(days=7)
    spent = _listed(rollup=[], runs=[], pushed=pushed, reopened=[NOW - timedelta(hours=2)])
    assert spent.ci_never_ran is True and spent.ci_start_spent is True
    # A reopen before this head was pushed belongs to an earlier head.
    earlier = _listed(rollup=[], runs=[], pushed=pushed, reopened=[pushed - timedelta(days=1)])
    assert earlier.ci_never_ran is True and earlier.ci_start_spent is False


def test_a_reported_check_costs_no_extra_forge_reads():
    rollup = [{"name": name, "status": "COMPLETED", "conclusion": "SUCCESS"} for name in REQUIRED]
    run, calls = _runner(rollup=rollup, runs=[], pushed=NOW - timedelta(days=7))
    pull = GitHubForge(run, ci_grace_s=GRACE, now=lambda: NOW).list_open(REPO)[0]
    assert pull.checks == "passing" and pull.ci_never_ran is False
    assert not [args for args in calls if "/actions/runs?" in args[-1] or "/commits/" in args[-1]]


def test_the_forge_reads_fork_and_auto_merge():
    pull = _listed(rollup=[], runs=[], pushed=NOW - timedelta(days=7), fork=True, auto_merge=True)
    assert pull.is_fork is True and pull.auto_merge is True


# --- config ----------------------------------------------------------------


def test_the_grace_period_is_a_lander_knob_with_a_thirty_minute_default():
    assert LanderConfig().ci_start_grace_minutes == 30
    parsed = parse_config(
        '[ci_start]\ngrace_minutes = 45\ntoken_env = "CI_START_TOKEN"\n[raci]\nauto_allowed = ["ci_start"]\n'
    )
    assert parsed.ci_start_grace_minutes == 45
    assert parsed.ci_start_token_env == "CI_START_TOKEN"
    assert "ci_start" in parsed.auto_allowed


def test_a_bad_grace_period_or_token_name_is_refused():
    for text in ("[ci_start]\ngrace_minutes = 0\n", '[ci_start]\ntoken_env = "ghp_abc"\n'):
        try:
            parse_config(text)
        except LanderRefused:
            continue
        raise AssertionError(f"accepted {text!r}")


# --- act -------------------------------------------------------------------


class Effects:
    def __init__(self, *, fail: str = "") -> None:
        self.sha = HEAD
        self.started: list[tuple[int, str, str]] = []
        self.added: list[tuple[int, str]] = []
        self.removed: list[tuple[int, str]] = []
        self.landed: list[int] = []
        self.fail = fail

    def head(self, number: int) -> str:
        del number
        return self.sha

    def apply(self, number: int, fix: Fix, head_sha: str) -> str:
        raise AssertionError("nothing is applied in these tests")

    def rerun(self, number: int, sha: str) -> str:
        del number, sha
        return "passing"

    def land(self, number: int, sha: str, method: str) -> None:
        del sha, method
        self.landed.append(number)

    def close(self, number: int, sha: str) -> None:
        raise AssertionError("nothing is closed for good in these tests")

    def add_label(self, number: int, label: str) -> None:
        self.added.append((number, label))

    def remove_label(self, number: int, label: str) -> None:
        self.removed.append((number, label))

    def start_ci(self, number: int, sha: str, *, token_env: str = "") -> None:
        if self.fail:
            raise LanderRefused(self.fail)
        self.started.append((number, sha, token_env))


class Fixes:
    name = "fake"

    def diagnose(self, pull: OpenPull) -> Diagnosis:
        if pull.checks == "passing":
            return Diagnosis("clear", "green", pull.files)
        return Diagnosis("unknown", "not green", pull.files)

    def draft(self, diagnosis: Diagnosis, pull: OpenPull) -> Fix:
        del pull
        if diagnosis.kind == "clear":
            return Fix(summary="land", kind="land")
        return Fix(summary="hold", needs_human=True, reason="no fixer configured")


def _config(*allowed: str, **kwargs) -> LanderConfig:
    from dataclasses import replace

    return replace(LanderConfig(), approvers=("@ada:local",), auto_allowed=tuple(allowed), **kwargs)


def _stuck(**kwargs) -> OpenPull:
    base: dict = dict(
        number=7,
        title="change",
        head_sha=HEAD,
        head_ref="topic/one",
        files=("src/a.py",),
        checks="pending",
        mergeable=True,
        ci_never_ran=True,
        missing_checks=REQUIRED,
    )
    base.update(kwargs)
    return OpenPull(**base)


def _act(tmp_path: Path, pulls, effects, config=None, environ=None):
    return run_actor(
        pulls,
        repo=REPO,
        config=config or _config("ci_start"),
        state_dir=tmp_path,
        effects=effects,
        gate=ApprovalGate(InMemoryActionStore()),
        provider=Fixes(),
        environ=environ if environ is not None else {},
    )


def test_act_starts_ci_once_and_a_second_sweep_raises_it_to_a_person(tmp_path: Path):
    effects = Effects()
    first = _act(tmp_path, [_stuck()], effects)
    assert first.outcomes[0].status == "ci-started"
    assert effects.started == [(7, HEAD, "")]
    # The claim is held only while RIVET acts, and it is given back.
    assert effects.added == [(7, "rivet:claimed")]
    assert effects.removed == [(7, "rivet:claimed")]

    second = _act(tmp_path, [_stuck()], effects)
    assert effects.started == [(7, HEAD, "")], "one start per head"
    assert second.outcomes[0].status == "escalated"
    assert "already" in second.outcomes[0].reason
    assert len(effects.added) == 1, "a pull request RIVET only reports on is not claimed"
    lines = (tmp_path / "agents" / "rivet" / "lander.jsonl").read_text(encoding="utf-8").splitlines()
    raised = [json.loads(line) for line in lines if json.loads(line).get("action") == "needs-person"]
    assert raised and raised[-1]["pr"] == 7 and raised[-1]["category"] == CI_NEVER_RAN


def test_a_new_head_gets_its_own_one_start(tmp_path: Path):
    effects = Effects()
    _act(tmp_path, [_stuck()], effects)
    effects.sha = "b" * 40
    again = _act(tmp_path, [_stuck(head_sha="b" * 40)], effects)
    assert again.outcomes[0].status == "ci-started"
    assert [item[1] for item in effects.started] == [HEAD, "b" * 40]


def test_starts_stop_at_the_try_cap_whatever_the_head(tmp_path: Path):
    effects = Effects()
    config = _config("ci_start", repair_cap=2)
    for index in range(3):
        sha = str(index) * 40
        effects.sha = sha
        result = _act(tmp_path, [_stuck(head_sha=sha)], effects, config=config)
    assert len(effects.started) == 2
    assert result.outcomes[0].status == "escalated"
    assert "cap" in result.outcomes[0].reason


def test_a_start_the_forge_already_shows_is_spent_with_no_local_record(tmp_path: Path):
    effects = Effects()
    result = _act(tmp_path, [_stuck(ci_start_spent=True)], effects)
    assert effects.started == []
    assert result.outcomes[0].status == "escalated"


def test_the_kill_switch_a_fork_a_draft_and_an_armed_auto_merge_are_respected(tmp_path: Path):
    killed = Effects()
    stopped = _act(tmp_path, [_stuck()], killed, environ={"AXIOM_LANDER_KILL": "1"})
    assert stopped.stopped == "kill-switch" and killed.started == [] and killed.added == []

    for kwargs, word in (
        ({"is_fork": True}, "fork"),
        ({"draft": True}, "draft"),
        ({"auto_merge": True}, "auto-merge"),
    ):
        effects = Effects()
        result = _act(tmp_path, [_stuck(**kwargs)], effects)
        assert effects.started == [], kwargs
        assert effects.added == [] and effects.removed == [], kwargs
        assert word in result.outcomes[0].reason


def test_starting_ci_is_off_until_the_repo_allows_it_and_needs_its_token(tmp_path: Path):
    off = Effects()
    result = _act(tmp_path, [_stuck()], off, config=_config("merge"))
    assert off.started == [] and result.outcomes[0].status == "escalated"

    config = _config("ci_start", ci_start_token_env="CI_START_TOKEN")
    absent = Effects()
    result = _act(tmp_path, [_stuck()], absent, config=config, environ={})
    assert absent.started == [] and "CI_START_TOKEN" in result.outcomes[0].reason

    present = Effects()
    _act(tmp_path, [_stuck()], present, config=config, environ={"CI_START_TOKEN": "x"})
    assert present.started == [(7, HEAD, "CI_START_TOKEN")]


def test_a_failed_start_is_still_spent_and_goes_to_a_person(tmp_path: Path):
    effects = Effects(fail="closed #7 and could not reopen it")
    first = _act(tmp_path, [_stuck()], effects)
    assert first.outcomes[0].status == "escalated"
    assert "could not reopen" in first.outcomes[0].reason
    assert effects.removed == [(7, "rivet:claimed")]
    assert ci_start.spent(tmp_path, REPO, 7, HEAD)


def test_the_start_is_a_close_and_a_reopen_with_no_force_and_no_comment():
    assert close_command(REPO, 7) == ["gh", "pr", "close", "7", "--repo", REPO]
    assert reopen_command(REPO, 7) == ["gh", "pr", "reopen", "7", "--repo", REPO]


# --- report ----------------------------------------------------------------


def _report(tmp_path: Path, pull: OpenPull, config=None):
    return build_report(
        [pull],
        repo=REPO,
        config=config or _config("ci_start"),
        state_dir=tmp_path,
        tier_map=None,
        selector=_All(),
    )


class _All:
    def select(self, paths, base, head):
        from axiom.extensions.builtins.release.selectors import Selection

        del paths, base, head
        return Selection(True, (), "all")


def test_the_report_gives_it_its_own_reason_and_names_the_missing_checks(tmp_path: Path):
    report = _report(tmp_path, _stuck())
    assert [item.number for item in report.waiting_on_ci] == []
    held = report.needs_human[0]
    assert CI_NEVER_RAN in held.reasons and "checks_unknown" not in held.reasons
    row = report.to_dict()["ci_never_ran"][0]
    assert row["number"] == 7 and row["next"] == "rivet"
    assert row["missing_checks"] == list(REQUIRED)
    text = render_text(report)
    assert "CI never ran (1):" in text
    line = next(line for line in text.splitlines() if line.startswith("  #7 no workflow run"))
    for name in REQUIRED:
        assert name in line
    assert "RIVET starts it once" in line


def test_a_spent_start_moves_the_reason_to_needs_a_person(tmp_path: Path):
    ci_start.record_start(tmp_path, repo=REPO, number=7, head_sha=HEAD)
    report = _report(tmp_path, _stuck())
    held = report.needs_human[0]
    assert CI_NEVER_RAN_PERSON in held.reasons and CI_NEVER_RAN not in held.reasons
    row = report.to_dict()["ci_never_ran"][0]
    assert row["next"] == "person"
    assert "needs a person" in render_text(report)


def test_a_draft_is_reported_and_left_to_its_author(tmp_path: Path):
    report = _report(tmp_path, _stuck(draft=True))
    held = report.needs_human[0]
    assert "draft" in held.reasons and CI_NEVER_RAN in held.reasons
    assert report.to_dict()["ci_never_ran"][0]["next"] == "author"
    assert "marking it ready starts CI" in render_text(report)


def test_checks_pending_for_any_other_cause_is_untouched(tmp_path: Path):
    report = _report(tmp_path, _stuck(ci_never_ran=False, missing_checks=()))
    assert [item.number for item in report.waiting_on_ci] == [7]
    assert report.to_dict()["ci_never_ran"] == []
    assert "CI never ran" not in render_text(report)


# --- the claim label -------------------------------------------------------


def test_a_sweep_over_drafts_and_report_only_pull_requests_makes_no_label_call(tmp_path: Path):
    pulls = [
        _stuck(number=1, ci_never_ran=False, draft=True, checks="passing"),
        _stuck(number=2, ci_never_ran=False, checks="failing"),  # no fixer: escalated
        _stuck(number=3, ci_never_ran=False, checks="pending"),
        _stuck(number=4, ci_never_ran=False, checks="passing", files=("migrations/0001.sql",)),
        _stuck(number=5, draft=True),
    ]
    effects = Effects()
    result = _act(tmp_path, pulls, effects, config=_config("merge", "ci_start"))
    assert len(result.outcomes) == 5
    assert effects.landed == []
    assert effects.added == [] and effects.removed == []


def test_a_pull_request_rivet_lands_is_claimed_for_the_landing_only(tmp_path: Path):
    effects = Effects()
    result = _act(tmp_path, [_stuck(ci_never_ran=False, checks="passing")], effects, config=_config("merge"))
    assert result.outcomes[0].status == "landed"
    assert effects.added == [(7, "rivet:claimed")]
    assert effects.removed == [(7, "rivet:claimed")]


def test_a_label_left_by_an_earlier_sweep_is_removed_when_the_action_has_ended(tmp_path: Path):
    effects = Effects()
    stale = _stuck(ci_never_ran=False, checks="failing", labels=("rivet:claimed",))
    _act(tmp_path, [stale], effects, config=_config("merge"))
    assert effects.added == []
    assert effects.removed == [(7, "rivet:claimed")]


# --- this checkout's wiring ------------------------------------------------


def test_this_checkout_allows_the_start_and_its_workflow_hands_in_a_token_that_can():
    root = Path(__file__).resolve().parents[6]
    config = parse_config((root / ".axiom" / "release-lander.toml").read_text(encoding="utf-8"))
    assert "ci_start" in config.auto_allowed
    assert config.ci_start_grace_minutes == 30
    workflow = (root / ".github" / "workflows" / "rivet-lander.yml").read_text(encoding="utf-8")
    # The job token starts no workflow, so the start must not fall back to it.
    assert f"{config.ci_start_token_env}: ${{{{ steps.rivet.outputs.token }}}}" in workflow
    assert "create-github-app-token" in workflow
    # Pull-request triggers stay as they are: an `edited` trigger would let a title
    # edit cancel a running suite and report its skipped jobs as passing.
    for name in ("ci.yml", "loop-guards.yml"):
        text = (root / ".github" / "workflows" / name).read_text(encoding="utf-8")
        assert "types: [opened, synchronize, reopened, ready_for_review]" in text
        assert "edited" not in text.split("jobs:", 1)[0]
