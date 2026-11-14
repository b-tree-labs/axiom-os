# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Head-run truth: a head is judged by its own run, never by the last completed one.

Main's concurrency group keeps one running and one pending run, so a burst of
merges leaves heads with no run of their own (#1782). Every fixture here is a
run list served through an injected client; nothing touches a network.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from pathlib import Path

import pytest

from axiom.extensions.builtins.release.ci_health.client import CIClientError
from axiom.extensions.builtins.release.ci_health.config import CIHealthConfig
from axiom.extensions.builtins.release.ci_health.head import (
    FAILURE,
    PENDING,
    SUCCESS,
    SUPERSEDED,
    UNKNOWN,
    HeadVerdict,
    head_verdict,
)
from axiom.extensions.builtins.release.ci_health.skill import ci_head
from axiom.infra.skills import SkillContext, SkillRegistry

FIX = Path(__file__).parent / "fixtures" / "ci_health"
NOW = datetime(2026, 11, 10, 21, 0, tzinfo=UTC)
REPO = "example-org/app"


def _load():
    return json.loads((FIX / "head_runs.json").read_text(encoding="utf-8"))


class HeadClient:
    """Serves ``head_runs.json``. Records what was asked. No network."""

    def __init__(self, *, branch_head: str = "h5", fail: bool = False) -> None:
        self.branch_head_sha = branch_head
        self.fail = fail
        self.asked: list[str] = []
        self.ancestors = {("h2", "h3"), ("h1", "h3")}

    def workflow_runs(self, repo: str, workflow: str, branch: str, *, per_page: int = 30) -> list[dict]:
        self.asked.append(f"runs:{workflow}@{branch}")
        if self.fail:
            raise CIClientError("HTTP 500")
        return [r for r in _load()["workflow_runs"] if r["head_branch"] == branch]

    def branch_head(self, repo: str, branch: str) -> str:
        self.asked.append(f"head:{branch}")
        return self.branch_head_sha

    def jobs(self, repo: str, run_id: int) -> list[dict]:
        self.asked.append(f"jobs:{run_id}")
        return _load()["jobs"].get(str(run_id), [])

    def is_ancestor(self, repo: str, older_sha: str, newer_sha: str) -> bool:
        return (older_sha, newer_sha) in self.ancestors


def _ctx(tmp_path: Path) -> SkillContext:
    return SkillContext(registry=SkillRegistry(), state_dir=tmp_path, logger=logging.getLogger("t"))


# -- the four verdicts ---------------------------------------------------------


def test_a_head_whose_own_run_passed_is_green_even_on_a_second_attempt():
    verdict = head_verdict(HeadClient(), CIHealthConfig(), REPO, sha="h3", now=NOW)
    assert verdict.verdict == SUCCESS
    assert verdict.green and not verdict.red
    assert verdict.run_id == 204 and verdict.run_attempt == 2 and verdict.after_rerun
    assert "green" in verdict.line() and "re-run" in verdict.line()


def test_a_head_whose_own_run_failed_is_red_and_names_the_failing_checks():
    client = HeadClient()
    verdict = head_verdict(client, CIHealthConfig(), REPO, sha="h1", now=NOW)
    assert verdict.verdict == FAILURE and verdict.red
    assert verdict.failing_checks == ["Unit Tests"], "a cancelled job is a consequence, not a failure"
    assert verdict.run_url.endswith("/runs/202")
    assert "Unit Tests" in verdict.line()
    # The newest completed head is still named, so a reader sees that main moved on.
    assert verdict.newest_completed_sha == "h3" and verdict.newest_completed_verdict == SUCCESS


def test_a_head_with_a_run_in_progress_is_pending():
    verdict = head_verdict(HeadClient(), CIHealthConfig(), REPO, sha="h4", now=NOW)
    assert verdict.verdict == PENDING
    assert verdict.run_id == 205
    assert not verdict.green and not verdict.red


def test_a_head_whose_pending_run_was_replaced_is_superseded_and_names_the_newest_completed_head():
    client = HeadClient()
    verdict = head_verdict(client, CIHealthConfig(), REPO, sha="h2", now=NOW)
    assert verdict.verdict == SUPERSEDED
    assert verdict.newest_completed_sha == "h3"
    assert verdict.newest_completed_verdict == SUCCESS
    assert verdict.newest_completed_url.endswith("/runs/204")
    assert verdict.newest_contains_head is True
    assert not verdict.green and not verdict.red, "untested is neither green nor red"
    assert "h3" in verdict.line() and "untested" in verdict.line()


def test_a_head_with_no_run_at_all_is_superseded_when_the_branch_moved_on():
    verdict = head_verdict(HeadClient(branch_head="h4"), CIHealthConfig(), REPO, sha="h9", now=NOW)
    assert verdict.verdict == SUPERSEDED
    assert verdict.newest_completed_sha == "h3"
    assert verdict.newest_contains_head is None, "the forge was not asked about a head it never ran"


def test_the_branch_head_with_no_run_yet_is_pending_not_superseded():
    client = HeadClient(branch_head="h5")
    verdict = head_verdict(client, CIHealthConfig(), REPO, now=NOW)
    assert verdict.sha == "h5", "no sha given: the forge's branch head is judged"
    assert verdict.verdict == PENDING
    assert "no run" in verdict.line()
    assert "head:main" in client.asked


def test_a_forge_that_cannot_be_read_is_unknown_and_claims_nothing():
    verdict = head_verdict(HeadClient(fail=True), CIHealthConfig(), REPO, sha="h3", now=NOW)
    assert verdict.verdict == UNKNOWN
    assert verdict.readable is False
    assert not verdict.green and not verdict.red
    assert "HTTP 500" in verdict.reasons[0]


def test_the_verdict_round_trips_as_a_dict_for_the_hook():
    verdict = head_verdict(HeadClient(), CIHealthConfig(), REPO, sha="h1", now=NOW)
    data = verdict.to_dict()
    assert data["verdict"] == FAILURE
    assert data["failing_checks"] == ["Unit Tests"]
    assert data["line"] == verdict.line()
    assert HeadVerdict(**{k: v for k, v in data.items() if k != "line"}).verdict == FAILURE


# -- the skill -------------------------------------------------------------


def test_the_ci_head_skill_answers_for_the_named_head_in_json(tmp_path: Path, capsys):
    result = ci_head(
        {"repo": REPO, "sha": "h2", "format": "json", "_client": HeadClient()}, _ctx(tmp_path)
    )
    assert result.ok
    printed = json.loads(capsys.readouterr().out)
    assert printed["verdict"] == SUPERSEDED
    assert printed["newest_completed_sha"] == "h3"
    assert result.value["verdict"] == SUPERSEDED


def test_the_ci_head_skill_fails_when_the_forge_cannot_be_read_so_a_caller_falls_back(tmp_path: Path):
    result = ci_head({"repo": REPO, "_client": HeadClient(fail=True), "quiet": True}, _ctx(tmp_path))
    assert not result.ok
    assert result.value["verdict"] == UNKNOWN


def test_the_ci_head_skill_needs_a_repo(tmp_path: Path):
    result = ci_head({"_client": HeadClient(), "quiet": True}, _ctx(tmp_path))
    assert not result.ok and "repo" in result.errors[0]


@pytest.mark.parametrize("verb", ["ci-head"])
def test_the_cli_has_the_verb_and_it_needs_no_checkout(verb):
    from axiom.extensions.builtins.release.cli import _needs_repo, build_parser

    args = build_parser().parse_args([verb, "--repo", REPO, "--sha", "h1", "--format", "json"])
    assert args.verb == verb and args.sha == "h1"
    assert not _needs_repo([verb, "--repo", REPO])


# -- the pre-push hook uses it, with a fallback --------------------------------


def test_the_pre_push_hook_asks_the_head_not_the_last_completed_run():
    root = Path(__file__).resolve().parents[6]
    hook = (root / "scripts" / "hooks" / "pre-push").read_text(encoding="utf-8")
    assert "release.cli ci-head" in hook, "the hook must judge main by its current head"
    assert "--format json" in hook
    # The old heuristic stays as the fallback when the skill cannot answer.
    assert "gh run list --branch main --workflow ci.yml --limit 1" in hook
    assert "superseded" in hook
