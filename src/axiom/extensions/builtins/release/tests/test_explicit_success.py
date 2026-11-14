# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Explicit success only: the lander merges when every required check says ``success``.

An empty check list is pending, not "not failed" (#1782). Every forge response
here is a fixture served through a fake runner; nothing touches a network.
"""

from __future__ import annotations

import json
from pathlib import Path

from axiom.extensions.builtins.release.lander.actor import GitEffects, run_actor
from axiom.extensions.builtins.release.lander.checks import (
    FAILURE,
    PENDING,
    SUCCESS,
    check_verdict,
    required_contexts,
)
from axiom.extensions.builtins.release.lander.config import parse_config
from axiom.extensions.builtins.release.lander.model import OpenPull
from axiom.infra.orchestrator.approval import ApprovalGate
from axiom.infra.orchestrator.approval_store import InMemoryActionStore

REQUIRED = ("Lint", "Unit Tests")


def _check(name: str, conclusion: str | None, status: str = "COMPLETED") -> dict:
    row: dict = {"__typename": "CheckRun", "name": name, "status": status}
    if conclusion is not None:
        row["conclusion"] = conclusion
    return row


# -- the verdict ----------------------------------------------------------------


def test_every_required_check_success_is_success():
    verdict = check_verdict([_check("Lint", "SUCCESS"), _check("Unit Tests", "SUCCESS")], REQUIRED)
    assert verdict.state == SUCCESS
    assert verdict.failing == () and verdict.pending == () and verdict.missing == ()


def test_an_empty_check_list_is_pending_never_not_failed():
    """The negative control: nothing reported must not merge."""
    verdict = check_verdict([], REQUIRED)
    assert verdict.state == PENDING
    assert verdict.missing == REQUIRED
    assert "no check" in verdict.reason() or "not reported" in verdict.reason()
    # Even with no protection to compare against, nothing reported is pending.
    assert check_verdict([], ()).state == PENDING
    assert check_verdict(None, ()).state == PENDING


def test_a_required_check_that_has_not_registered_is_pending():
    verdict = check_verdict([_check("Lint", "SUCCESS")], REQUIRED)
    assert verdict.state == PENDING
    assert verdict.missing == ("Unit Tests",)


def test_a_required_check_still_running_is_pending():
    verdict = check_verdict([_check("Lint", "SUCCESS"), _check("Unit Tests", None, "IN_PROGRESS")], REQUIRED)
    assert verdict.state == PENDING
    assert verdict.pending == ("Unit Tests",)


def test_a_failed_required_check_is_failure_and_names_it():
    verdict = check_verdict([_check("Lint", "SUCCESS"), _check("Unit Tests", "FAILURE")], REQUIRED)
    assert verdict.state == FAILURE
    assert verdict.failing == ("Unit Tests",)
    assert "Unit Tests" in verdict.reason()


def test_skipped_and_neutral_are_not_success():
    assert check_verdict([_check("Lint", "SKIPPED"), _check("Unit Tests", "SUCCESS")], REQUIRED).state == PENDING
    assert check_verdict([_check("Lint", "NEUTRAL"), _check("Unit Tests", "SUCCESS")], REQUIRED).state == PENDING


def test_failure_outranks_pending():
    rows = [_check("Lint", "FAILURE"), _check("Unit Tests", None, "QUEUED")]
    assert check_verdict(rows, REQUIRED).state == FAILURE


def test_with_no_protection_every_reported_check_must_be_success():
    assert check_verdict([_check("a", "SUCCESS"), _check("b", "SUCCESS")], ()).state == SUCCESS
    assert check_verdict([_check("a", "SUCCESS"), _check("b", None, "PENDING")], ()).state == PENDING
    assert check_verdict([_check("a", "SUCCESS"), _check("b", "FAILURE")], ()).state == FAILURE


def test_a_commit_status_context_counts_like_a_check_run():
    rows = [
        {"__typename": "StatusContext", "context": "Lint", "state": "SUCCESS"},
        {"__typename": "StatusContext", "context": "Unit Tests", "state": "PENDING"},
    ]
    verdict = check_verdict(rows, REQUIRED)
    assert verdict.state == PENDING and verdict.pending == ("Unit Tests",)


def test_an_extra_unrequired_failing_check_does_not_block_when_the_required_set_is_green():
    rows = [_check("Lint", "SUCCESS"), _check("Unit Tests", "SUCCESS"), _check("Nightly", "FAILURE")]
    assert check_verdict(rows, REQUIRED).state == SUCCESS


# -- the required set, from branch protection and rulesets --------------------------


def test_required_contexts_come_from_protection_contexts_or_checks():
    legacy = {"required_status_checks": {"strict": True, "contexts": ["Lint", "Unit Tests"]}}
    assert required_contexts(legacy, []) == REQUIRED
    modern = {"required_status_checks": {"checks": [{"context": "Unit Tests", "app_id": 15368}, {"context": "Lint"}]}}
    assert required_contexts(modern, []) == REQUIRED


def test_required_contexts_come_from_rulesets_too_and_merge_without_repeats():
    rules = [
        {"type": "pull_request", "parameters": {}},
        {"type": "required_status_checks", "parameters": {"required_status_checks": [{"context": "Unit Tests"}, {"context": "Smoke"}]}},
    ]
    protection = {"required_status_checks": {"contexts": ["Lint", "Unit Tests"]}}
    assert required_contexts(protection, rules) == ("Lint", "Smoke", "Unit Tests")
    assert required_contexts(None, rules) == ("Smoke", "Unit Tests")
    assert required_contexts(None, None) == ()


# -- the effects the actor lands through ---------------------------------------------


class Runner:
    """Serves ``gh`` answers by command prefix. Records every call. No network."""

    def __init__(self, *, rollup, head: str = "abc", protection=None, rules=None) -> None:
        self.rollup = rollup
        self.head = head
        self.protection = protection
        self.rules = rules if rules is not None else []
        self.calls: list[list[str]] = []

    def __call__(self, args: list[str]) -> str:
        self.calls.append(list(args))
        joined = " ".join(args)
        if "pr view" in joined:
            return json.dumps({"headRefOid": self.head, "baseRefName": "main", "statusCheckRollup": self.rollup})
        if "/protection" in joined:
            if self.protection is None:
                raise RuntimeError("HTTP 404: Branch not protected")
            return json.dumps(self.protection)
        if "/rules/branches/" in joined:
            return json.dumps(self.rules)
        if "pr merge" in joined:
            return ""
        raise AssertionError(f"unexpected command: {joined}")


def _effects(runner: Runner) -> GitEffects:
    effects = GitEffects(repo="acme/widgets", token_env="GH_TOKEN")
    effects._run = runner  # type: ignore[method-assign]
    return effects


PROTECTION = {"required_status_checks": {"strict": False, "contexts": ["Lint", "Unit Tests"]}}


def test_the_effects_report_passing_only_when_the_required_checks_all_succeeded():
    runner = Runner(rollup=[_check("Lint", "SUCCESS"), _check("Unit Tests", "SUCCESS")], protection=PROTECTION)
    assert _effects(runner).rerun(4, "abc") == "passing"


def test_the_effects_report_pending_on_an_empty_rollup_even_when_protection_is_unreadable():
    """Negative control at the merge door: nothing reported is pending, never passing."""
    runner = Runner(rollup=[], protection=None)
    effects = _effects(runner)
    assert effects.rerun(4, "abc") == "pending"
    runner.rollup = None
    assert effects.rerun(4, "abc") == "pending"


def test_the_effects_report_pending_when_a_required_check_has_not_registered():
    runner = Runner(rollup=[_check("Lint", "SUCCESS")], protection=PROTECTION)
    assert _effects(runner).rerun(4, "abc") == "pending"


def test_the_effects_report_failing_when_a_required_check_failed():
    runner = Runner(rollup=[_check("Lint", "SUCCESS"), _check("Unit Tests", "FAILURE")], protection=PROTECTION)
    assert _effects(runner).rerun(4, "abc") == "failing"


def test_a_moved_head_is_unknown_and_the_required_set_is_read_once_per_base():
    runner = Runner(rollup=[_check("Lint", "SUCCESS"), _check("Unit Tests", "SUCCESS")], protection=PROTECTION)
    effects = _effects(runner)
    assert effects.rerun(4, "abc") == "passing"
    assert effects.rerun(4, "abc") == "passing"
    assert sum(1 for call in runner.calls if "/protection" in " ".join(call)) == 1
    assert effects.rerun(4, "moved") == "unknown"


# -- the actor, end to end: an empty check list must not merge --------------------------


class EmptyRollupEffects:
    """A head whose required test has not registered yet."""

    def __init__(self) -> None:
        self.landed: list[tuple[int, str, str]] = []
        self.runner = Runner(rollup=[], protection=PROTECTION)
        self.effects = GitEffects(repo="acme/widgets", token_env="GH_TOKEN")
        self.effects._run = self.runner  # type: ignore[method-assign]

    def head(self, number: int) -> str:
        return "abc"

    def apply(self, number: int, fix, head_sha: str) -> str:
        raise AssertionError("nothing to apply")

    def rerun(self, number: int, sha: str) -> str:
        return self.effects.rerun(number, sha)

    def land(self, number: int, sha: str, method: str) -> None:
        self.landed.append((number, sha, method))

    def close(self, number: int, sha: str) -> None:
        pass


class ClearFixes:
    name = "clear"

    def diagnose(self, pull):
        from axiom.extensions.builtins.release.fixes import Diagnosis

        return Diagnosis("clear", "green", pull.files)

    def draft(self, diagnosis, pull):
        from axiom.extensions.builtins.release.fixes import Fix

        return Fix(summary="land", kind="land")


def test_the_actor_does_not_merge_a_pull_request_whose_check_list_is_empty(tmp_path: Path):
    effects = EmptyRollupEffects()
    pull = OpenPull(number=4, title="x", head_sha="abc", head_ref="t/one", files=("src/a.py",), checks="passing", mergeable=True)
    config = parse_config('mode = "warn"\n')
    from dataclasses import replace

    config = replace(config, approvers=("@owner",), auto_allowed=("merge",))
    result = run_actor(
        [pull],
        repo="acme/widgets",
        config=config,
        state_dir=tmp_path,
        effects=effects,
        gate=ApprovalGate(InMemoryActionStore()),
        provider=ClearFixes(),
    )
    assert effects.landed == [], "an empty check list is pending; nothing merges"
    outcome = result.outcomes[0]
    assert outcome.status == "verified" and outcome.reason == "pending"
    assert not any("pr merge" in " ".join(call) for call in effects.runner.calls)
