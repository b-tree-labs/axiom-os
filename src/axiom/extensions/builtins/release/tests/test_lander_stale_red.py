# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""A pull request whose red result predates main's last green run is refreshed, with a cap.

On 2026-11-22 five pull requests showed Unit Tests red for hours. Main itself had been red:
another change broke a guard, every pull request inherited the failure, and once main was fixed
nothing re-ran them. The result was stale, not wrong. RIVET now brings such a branch up to date
once main has been green since the failure, so fresh CI decides.

Bounded so it never loops on a pull request that really fails: the failure must be at least six
hours old, which allows one refresh per pull request per six hours, and the refreshes share the
cap of three per pass with the cancelled-checks refresh.
"""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

from axiom.extensions.builtins.release.fixes import MechanicalFixProvider
from axiom.extensions.builtins.release.lander.actor import GitEffects, run_actor
from axiom.extensions.builtins.release.lander.adapters import failing_checks_from_rollup
from axiom.extensions.builtins.release.lander.config import parse_config
from axiom.extensions.builtins.release.lander.model import FailingCheck, OpenPull
from axiom.infra.orchestrator.approval import ApprovalGate
from axiom.infra.orchestrator.approval_store import InMemoryActionStore

NOW = datetime.now(UTC)


def _iso(delta: timedelta) -> str:
    return (NOW - delta).strftime("%Y-%m-%dT%H:%M:%SZ")


def _pull(number: int, failed_ago: timedelta, conclusion: str = "FAILURE") -> OpenPull:
    return OpenPull(
        number=number, title="change", head_sha=f"sha{number}", head_ref=f"topic/{number}",
        files=("src/a.py",), checks="failing", failure_log="", mergeable=True,
        failing_checks=(FailingCheck("Unit Tests", conclusion=conclusion, completed_at=_iso(failed_ago)),),
    )


class _Effects:
    def __init__(self, green_ago: timedelta | None) -> None:
        self.green = None if green_ago is None else NOW - green_ago
        self.updated: list[int] = []

    def main_green_at(self, base: str):
        return self.green

    def head(self, number: int) -> str:
        return f"sha{number}"

    def update_branch(self, number: int, sha: str) -> str:
        self.updated.append(number)
        return f"new{number}"

    def apply(self, *a):  # pragma: no cover
        raise AssertionError("not a repair")

    def rerun(self, *a):  # pragma: no cover
        raise AssertionError("not a rerun")

    def land(self, *a):  # pragma: no cover
        raise AssertionError("not green")

    def close(self, *a):  # pragma: no cover
        raise AssertionError("not closed")


class _NoMainGreen(_Effects):
    main_green_at = None  # type: ignore[assignment]


def _run(pulls, tmp_path: Path, effects, *, auto=("merge", "update_branch")):
    config = replace(parse_config('mode = "warn"\n'), approvers=("@ada:local",), auto_allowed=auto)
    return run_actor(
        pulls, repo="o/r", config=config, state_dir=tmp_path, effects=effects,
        gate=ApprovalGate(InMemoryActionStore()), provider=MechanicalFixProvider(),
    )


def test_sr1_the_rollup_keeps_when_a_failing_check_finished():
    rows = [{"name": "Unit Tests", "status": "COMPLETED", "conclusion": "FAILURE",
             "completedAt": "2026-11-22T04:00:00Z"}]
    assert failing_checks_from_rollup(rows)[0].completed_at == "2026-11-22T04:00:00Z"


def test_sr2_a_failure_older_than_main_green_and_six_hours_is_refreshed(tmp_path):
    effects = _Effects(green_ago=timedelta(hours=1))
    result = _run([_pull(4, failed_ago=timedelta(hours=10))], tmp_path, effects)
    assert effects.updated == [4]
    assert result.outcomes[0].status == "updated"
    assert "stale" in result.outcomes[0].reason


def test_sr3_a_recent_failure_is_left_alone(tmp_path):
    effects = _Effects(green_ago=timedelta(minutes=5))
    _run([_pull(4, failed_ago=timedelta(hours=1))], tmp_path, effects)
    assert effects.updated == []


def test_sr4_a_failure_newer_than_main_green_is_real(tmp_path):
    """The pull request failed after main last went green: that result is fresh."""
    effects = _Effects(green_ago=timedelta(hours=20))
    result = _run([_pull(4, failed_ago=timedelta(hours=10))], tmp_path, effects)
    assert effects.updated == []
    assert result.outcomes[0].status != "updated"


def test_sr5_unknown_main_state_never_refreshes(tmp_path):
    for effects in (_Effects(green_ago=None), _NoMainGreen(green_ago=None)):
        _run([_pull(4, failed_ago=timedelta(hours=10))], tmp_path, effects)
        assert effects.updated == []


def test_sr6_without_update_branch_allowed_it_changes_nothing(tmp_path):
    effects = _Effects(green_ago=timedelta(hours=1))
    result = _run([_pull(4, failed_ago=timedelta(hours=10))], tmp_path, effects, auto=("merge",))
    assert effects.updated == []
    assert result.outcomes[0].status != "updated"


def test_sr7_at_most_three_are_refreshed_in_one_pass(tmp_path):
    effects = _Effects(green_ago=timedelta(hours=1))
    result = _run([_pull(n, timedelta(hours=10)) for n in range(10, 16)], tmp_path, effects)
    assert len(effects.updated) == 3
    assert sorted(o.status for o in result.outcomes).count("updated") == 3


def test_sr8_main_green_is_the_newest_commit_whose_checks_all_passed(monkeypatch):
    payload = {"data": {"repository": {"ref": {"target": {"history": {"nodes": [
        {"oid": "c3", "statusCheckRollup": {"state": "PENDING", "contexts": {"nodes": [
            {"__typename": "CheckRun", "completedAt": None}]}}},
        {"oid": "c2", "statusCheckRollup": {"state": "SUCCESS", "contexts": {"nodes": [
            {"__typename": "CheckRun", "completedAt": "2026-11-22T06:10:00Z"},
            {"__typename": "CheckRun", "completedAt": "2026-11-22T06:31:00Z"},
            {"__typename": "StatusContext"}]}}},
        {"oid": "c1", "statusCheckRollup": {"state": "SUCCESS", "contexts": {"nodes": [
            {"__typename": "CheckRun", "completedAt": "2026-11-21T01:00:00Z"}]}}},
    ]}}}}}}
    effects = GitEffects(repo="o/r", token_env="GH_TOKEN")
    monkeypatch.setattr(GitEffects, "_run", lambda self, args, **kw: json.dumps(payload))
    assert effects.main_green_at("main") == datetime(2026, 11, 22, 6, 31, tzinfo=UTC)
    monkeypatch.setattr(GitEffects, "_run", lambda self, args, **kw: "not json")
    assert effects.main_green_at("main") is None
