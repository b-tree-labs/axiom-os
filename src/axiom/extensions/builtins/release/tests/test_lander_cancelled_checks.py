# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""A pull request whose only red checks were cancelled is refreshed, not escalated.

A cancelled check never reported a result: a superseded run, a lost runner, a
concurrency group. On 2026-11-22 a dozen pull requests had sat for a month with
a CANCELLED check from the day they were opened. RIVET called each "unknown" and
sent it to the owner, because there was no log to diagnose. The move is to bring
the branch up to date so fresh CI decides, a few per pass so CI is not stormed.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from axiom.extensions.builtins.release.fixes import MechanicalFixProvider
from axiom.extensions.builtins.release.lander.actor import run_actor
from axiom.extensions.builtins.release.lander.adapters import failing_checks_from_rollup
from axiom.extensions.builtins.release.lander.config import parse_config
from axiom.extensions.builtins.release.lander.model import FailingCheck, OpenPull
from axiom.infra.orchestrator.approval import ApprovalGate
from axiom.infra.orchestrator.approval_store import InMemoryActionStore


def _pull(number: int = 4, *conclusions: str, mergeable: bool | None = True) -> OpenPull:
    return OpenPull(
        number=number,
        title="change",
        head_sha=f"sha{number}",
        head_ref=f"topic/{number}",
        files=("src/a.py",),
        checks="failing",
        failure_log="",
        mergeable=mergeable,
        failing_checks=tuple(FailingCheck(f"check {i}", conclusion=c) for i, c in enumerate(conclusions)),
    )


class _Effects:
    def __init__(self) -> None:
        self.updated: list[tuple[int, str]] = []

    def head(self, number: int) -> str:
        return f"sha{number}"

    def update_branch(self, number: int, sha: str) -> str:
        self.updated.append((number, sha))
        return f"new{number}"

    def apply(self, number, fix, head_sha):  # pragma: no cover - never reached
        raise AssertionError("a cancelled check is not repaired with a diff")

    def rerun(self, number, sha):  # pragma: no cover
        raise AssertionError("a month-old run cannot be rerun")

    def land(self, number, sha, method):  # pragma: no cover
        raise AssertionError("not green")

    def close(self, number, sha):  # pragma: no cover
        raise AssertionError("not closed")


def _run(pulls, tmp_path: Path, *, auto=("merge", "update_branch")):
    config = replace(parse_config('mode = "warn"\n'), approvers=("@ada:local",), auto_allowed=auto)
    effects = _Effects()
    result = run_actor(
        pulls,
        repo="o/r",
        config=config,
        state_dir=tmp_path,
        effects=effects,
        gate=ApprovalGate(InMemoryActionStore()),
        provider=MechanicalFixProvider(),
    )
    return result, effects


def test_cc1_the_rollup_keeps_each_failing_checks_conclusion():
    rollup = [
        {"name": "a", "status": "COMPLETED", "conclusion": "CANCELLED"},
        {"name": "b", "status": "COMPLETED", "conclusion": "FAILURE"},
        {"name": "c", "status": "COMPLETED", "conclusion": "SUCCESS"},
    ]
    assert [(c.name, c.conclusion) for c in failing_checks_from_rollup(rollup)] == [
        ("a", "CANCELLED"),
        ("b", "FAILURE"),
    ]


def test_cc2_only_cancelled_checks_diagnose_as_cancelled():
    provider = MechanicalFixProvider()
    assert provider.diagnose(_pull(4, "CANCELLED", "CANCELLED")).kind == "cancelled_checks"
    assert provider.diagnose(_pull(4, "CANCELLED", "FAILURE")).kind != "cancelled_checks"
    assert provider.diagnose(_pull(4, "FAILURE")).kind != "cancelled_checks"


def test_cc3_a_conflict_still_comes_first():
    assert MechanicalFixProvider().diagnose(_pull(4, "CANCELLED", mergeable=False)).kind == "merge_conflict"


def test_cc4_the_branch_is_updated_so_fresh_ci_decides(tmp_path: Path):
    result, effects = _run([_pull(4, "CANCELLED")], tmp_path)
    assert effects.updated == [(4, "sha4")]
    out = result.outcomes[0]
    assert out.status == "updated"
    assert "cancelled" in out.reason


def test_cc5_without_update_branch_in_auto_allowed_it_waits_and_changes_nothing(tmp_path: Path):
    result, effects = _run([_pull(4, "CANCELLED")], tmp_path, auto=("merge",))
    assert effects.updated == []
    assert result.outcomes[0].status == "waiting"
    assert "update_branch" in result.outcomes[0].reason


def test_cc6_at_most_three_are_updated_in_one_pass(tmp_path: Path):
    result, effects = _run([_pull(n, "CANCELLED") for n in range(10, 15)], tmp_path)
    assert len(effects.updated) == 3
    statuses = sorted(o.status for o in result.outcomes)
    assert statuses.count("updated") == 3
    assert statuses.count("waiting") == 2
    assert all("next pass" in o.reason for o in result.outcomes if o.status == "waiting")


def test_cc7_a_real_failure_is_not_refreshed(tmp_path: Path):
    result, effects = _run([_pull(4, "FAILURE")], tmp_path)
    assert effects.updated == []
    assert result.outcomes[0].status != "updated"
