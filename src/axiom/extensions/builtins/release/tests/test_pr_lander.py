# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Warn-only pull-request lander: group, report, propose. Do not merge."""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from axiom.extensions.builtins.release.cli import build_parser
from axiom.extensions.builtins.release.lander.actor import merge_command, push_command
from axiom.extensions.builtins.release.lander.adapters import (
    ForgeUnavailable,
    GitHubForge,
    GitLabForge,
    default_gh,
    parse_pr,
)
from axiom.extensions.builtins.release.lander.config import parse_config
from axiom.extensions.builtins.release.lander.coordination import claim, held_by
from axiom.extensions.builtins.release.lander.grouping import branch_prefix, group_pulls
from axiom.extensions.builtins.release.lander.loop import flight_name, heartbeat_propose, note_approval, run_once
from axiom.extensions.builtins.release.lander.loop_guard import LanderRefused, refuse_remote_write
from axiom.extensions.builtins.release.lander.model import OpenPull
from axiom.extensions.builtins.release.lander.report import build_report
from axiom.extensions.builtins.release.lander.skill import land
from axiom.extensions.builtins.release.lander.status import advisory_status
from axiom.extensions.builtins.release.lander.tickets import GitHubIssueTickets, LinearTickets
from axiom.extensions.builtins.release.pr_check_auto_closer import StaleIssue, _do_close
from axiom.infra.skills import SkillContext, SkillRegistry
from axiom.infra.switchplan.lock import SingleFlight
from axiom.policy.agent_action_guard import AgentAction, guarded_act, human_route

T0 = datetime(2026, 10, 25, 12, 0, tzinfo=UTC)


def _pull(**kwargs) -> OpenPull:
    base = dict(
        number=1,
        title="change",
        head_sha="abc",
        head_ref="cursor/one",
        updated_at=T0,
        created_at=T0,
        checks="passing",
        mergeable=True,
        files=("src/a.py",),
        draft=False,
    )
    base.update(kwargs)
    return OpenPull(**base)


def _ctx(tmp_path: Path) -> SkillContext:
    return SkillContext(
        registry=SkillRegistry(),
        state_dir=tmp_path,
        logger=logging.getLogger("lander-test"),
    )


def test_explicit_batch_label_ignores_the_window():
    early = _pull(number=1, labels=("batch:wave",), updated_at=T0)
    late = _pull(number=2, labels=("batch:wave",), updated_at=T0 + timedelta(days=2), head_sha="def")
    groups = group_pulls([early, late], window=timedelta(hours=6))
    assert len(groups) == 1
    assert {pull.number for pull in groups[0]} == {1, 2}


def test_session_ticket_and_prefix_respect_the_window():
    session_near = [
        _pull(number=1, labels=("session:s1",), head_ref="topic/a", files=("src/a.py",)),
        _pull(
            number=2,
            labels=("session:s1",),
            head_ref="topic/b",
            files=("src/b.py",),
            updated_at=T0 + timedelta(hours=5),
        ),
    ]
    assert len(group_pulls(session_near, window=timedelta(hours=6))) == 1
    session_far = [
        session_near[0],
        _pull(
            number=2,
            labels=("session:s1",),
            head_ref="topic/b",
            files=("src/b.py",),
            updated_at=T0 + timedelta(hours=7),
        ),
    ]
    assert len(group_pulls(session_far, window=timedelta(hours=6))) == 2

    tickets = [
        _pull(number=3, ticket_ids=("#9",), head_ref="topic/c", files=("src/c.py",)),
        _pull(
            number=4,
            ticket_ids=("#9",),
            head_ref="topic/d",
            files=("src/d.py",),
            updated_at=T0 + timedelta(hours=1),
        ),
    ]
    assert len(group_pulls(tickets, window=timedelta(hours=6))) == 1

    assert branch_prefix("cursor/one") == "cursor"
    assert branch_prefix("peng-alpha") == "peng"
    prefixes = [
        _pull(number=5, head_ref="cursor/one", files=("src/one.py",)),
        _pull(
            number=6,
            head_ref="cursor/two",
            files=("src/two.py",),
            updated_at=T0 + timedelta(hours=2),
        ),
        _pull(
            number=7,
            head_ref="peng-alpha",
            files=("src/peng.py",),
            updated_at=T0 + timedelta(hours=2),
        ),
    ]
    grouped = group_pulls(prefixes, window=timedelta(hours=6))
    sizes = sorted(len(group) for group in grouped)
    assert sizes == [1, 2]


def test_identical_files_group_and_a_subset_does_not():
    same = [
        _pull(number=1, files=("src/a.py", "src/b.py"), head_ref="other/a"),
        _pull(number=2, files=("src/b.py", "src/a.py"), head_ref="other/b", updated_at=T0 + timedelta(hours=1)),
    ]
    assert len(group_pulls(same, window=timedelta(hours=6))) == 1
    subset = [
        _pull(number=1, files=("src/a.py",), head_ref="left"),
        _pull(number=2, files=("src/a.py", "src/b.py"), head_ref="right", updated_at=T0 + timedelta(hours=1)),
    ]
    assert len(group_pulls(subset, window=timedelta(hours=6))) == 2


def test_grouped_dependabot_pulls_do_not_trip_batch_or_overlap():
    """Dependabot already grouped these. Rivet must not batch or overlap them.

    An explicit ``batch:`` label still joins them. An older single-package
    update is still superseded by the newer one, in the report test above.
    """
    pulls = [
        _pull(
            number=11,
            head_ref="dependabot/npm_and_yarn/frontend-aaa111bbb2",
            files=("web/package.json", "web/package-lock.json"),
            checks="passing",
            updated_at=T0,
        ),
        _pull(
            number=12,
            head_ref="dependabot/npm_and_yarn/frontend-dev-bbb222ccc3",
            files=("web/package.json",),
            checks="passing",
            updated_at=T0 + timedelta(hours=1),
        ),
        _pull(
            number=13,
            head_ref="dependabot/pip/python-ccc333ddd4",
            files=("pyproject.toml", "uv.lock"),
            checks="passing",
            updated_at=T0 + timedelta(hours=1),
        ),
        _pull(
            number=14,
            head_ref="dependabot/docker/runtime-ddd444eee5",
            files=("infra/Dockerfile", "deploy/docker-compose.yml"),
            checks="passing",
            updated_at=T0 + timedelta(minutes=30),
        ),
        _pull(
            number=15,
            head_ref="dependabot/pip/python-packages-eee555fff6",
            files=("pyproject.toml",),
            checks="passing",
            updated_at=T0 + timedelta(minutes=40),
        ),
    ]
    assert len(group_pulls(pulls, window=timedelta(hours=6))) == 5
    labeled = [
        _pull(
            number=21,
            head_ref="dependabot/pip/python-aaa",
            labels=("batch:deps",),
            files=("pyproject.toml",),
            checks="passing",
        ),
        _pull(
            number=22,
            head_ref="dependabot/npm_and_yarn/frontend-bbb",
            labels=("batch:deps",),
            files=("web/package.json",),
            checks="passing",
            updated_at=T0 + timedelta(days=3),
        ),
    ]
    assert len(group_pulls(labeled, window=timedelta(hours=6))) == 1
    same_files = [
        _pull(
            number=31,
            head_ref="dependabot/pip/prod-aaa111",
            files=("pyproject.toml", "uv.lock"),
            checks="passing",
        ),
        _pull(
            number=32,
            head_ref="dependabot/pip/dev-bbb222",
            files=("uv.lock", "pyproject.toml"),
            checks="passing",
            updated_at=T0 + timedelta(minutes=5),
        ),
    ]
    assert len(group_pulls(same_files, window=timedelta(hours=6))) == 2
    report = build_report(pulls + same_files, repo="acme/widgets", samples=[10])
    assert report.batches == []
    assert report.families == []
    reasons = [reason for item in report.needs_human for reason in item.reasons]
    assert not any(reason.startswith("file_overlap:") for reason in reasons)
    assert not any(reason.startswith("prefix:") for reason in reasons)
    assert report.superseded == []
    assert {item.number for item in report.needs_human} == {11, 12, 13, 14, 15, 31, 32}
    assert all("dependency" in item.reasons for item in report.needs_human)


def test_batch_exempt_stays_out_of_an_automatic_group():
    pulls = [
        _pull(number=1, head_ref="cursor/one", labels=("batch-exempt",)),
        _pull(number=2, head_ref="cursor/two"),
    ]
    assert len(group_pulls(pulls, window=timedelta(hours=6))) == 2


def test_ticket_adapters_read_github_and_linear():
    pull = _pull(
        title="Fixes acme/widgets#12",
        body="Linear: AB-9\nhttps://linear.app/acme/issue/AB-9\n",
        labels=("issue:4", "linear:CD-2"),
    )
    assert GitHubIssueTickets().ids_for(pull) == ("acme/widgets#12", "#4")
    assert "AB-9" in LinearTickets().ids_for(pull)
    assert "CD-2" in LinearTickets().ids_for(pull)


def test_report_lists_batches_closes_ready_and_human_decisions():
    pulls = [
        _pull(number=1, head_ref="cursor/one", files=("src/a.py",)),
        _pull(number=2, head_ref="cursor/two", files=("src/b.py",), updated_at=T0 + timedelta(hours=1)),
        _pull(
            number=3,
            head_ref="dependabot/pip/requests-2.31.0",
            files=("requirements/old.txt",),
            updated_at=T0,
        ),
        _pull(
            number=4,
            head_ref="dependabot/pip/requests-2.32.0",
            files=("requirements/new.txt",),
            head_sha="new",
            updated_at=T0 + timedelta(hours=1),
        ),
        _pull(number=5, head_ref="feature/auth", files=("src/auth/session.py",)),
        _pull(
            number=6,
            checks="failing",
            ci_attempts=3,
            failure_log="ruff found errors",
            head_ref="fix/again",
            files=("src/fix.py",),
        ),
        _pull(number=7, draft=True, head_ref="wip/draft", files=("src/draft.py",)),
        _pull(number=8, mergeable=False, head_ref="conflict/one", files=("src/conflict.py",)),
        _pull(number=9, checks="pending", head_ref="wait/ci", files=("src/wait.py",)),
        _pull(number=10, files=("src/a.py", "src/c.py"), head_ref="overlap/more"),
    ]
    report = build_report(pulls, repo="acme/widgets", samples=[15, 17, 19])
    assert report.suite_minutes == 17
    assert report.suite_minutes_source == "learned"
    dependabot_family = [
        family for family in report.families if 3 in family.members or 4 in family.members
    ]
    assert dependabot_family == []
    assert {item.number for item in report.superseded} == {3}
    assert report.superseded[0].keeper == 4
    assert report.superseded[0].reason == "superseded_dependency"
    ready = {item.number for item in report.ready}
    human = {item.number: item.reasons for item in report.needs_human}
    assert 1 in ready or 2 in ready
    assert 5 in human and "security" in human[5]
    assert 6 in human and "try-cap" in human[6]
    assert 7 in human and "draft" in human[7]
    assert 8 in human and "conflict" in human[8]
    assert any(reason.startswith("file_overlap:") for reason in human[1] + human.get(10, ()))
    assert {item.number for item in report.waiting_on_ci} == {9}
    assert report.minutes_saved >= report.suite_minutes
    assert report.to_dict()["actions_disabled"] == ["merge", "enqueue", "force_push", "status_post"]
    assert "author.py" not in " ".join(human_route(["src/author.py"]))


def test_approval_is_bound_to_the_head_and_a_new_sha_resets_it(tmp_path):
    pull = _pull(number=12, head_sha="sha-1")
    note_approval(tmp_path, repo="acme/widgets", number=12, head_sha="sha-1", by="person")
    bound = build_report(
        [pull],
        repo="acme/widgets",
        state_dir=tmp_path,
        approvals={12: "sha-1"},
    )
    assert bound.ready[0].approval == "bound"
    reset = build_report(
        [_pull(number=12, head_sha="sha-2")],
        repo="acme/widgets",
        approvals={12: "sha-1"},
    )
    assert reset.needs_human[0].approval == "reset"
    assert "approval_reset" in reset.needs_human[0].reasons


def test_a_pass_proposes_and_a_second_pass_is_busy(tmp_path):
    pulls = [
        _pull(number=1),
        _pull(number=2, head_ref="cursor/two", files=("src/b.py",), updated_at=T0 + timedelta(minutes=30)),
    ]
    result = run_once(repo="acme/widgets", state_dir=tmp_path, pulls=pulls, samples=[11])
    assert result.action == "propose"
    assert result.steps == ("batch", "review", "fix", "propose")
    assert result.merged is False
    assert result.enqueued is False
    assert result.status_posted is False
    line = json.loads((tmp_path / "agents" / "rivet" / "lander.jsonl").read_text().splitlines()[-1])
    assert line["action"] == "propose"
    assert line["merged"] is False
    assert line["enqueued"] is False
    assert line["status_posted"] is False
    assert list((tmp_path / "agents" / "rivet" / "lander-records").glob("*.json"))
    assert held_by(tmp_path, 1, actor="lander", repo="acme/widgets")

    lease = tmp_path / "agents" / "rivet" / "lander-leases"
    held = SingleFlight(flight_name("acme/widgets"), lease_dir=lease)
    assert held.acquire()
    skipped = run_once(repo="acme/widgets", state_dir=tmp_path, pulls=pulls)
    assert skipped.action == "skip"
    assert skipped.reason == "busy"
    held.release()


def test_the_closer_and_the_lander_do_not_both_take_a_pull_request(tmp_path, monkeypatch):
    monkeypatch.setenv("AXI_STATE_DIR", str(tmp_path))
    claim(tmp_path, repo="acme/widgets", number=7, actor="lander", head_sha="abc")
    blocked = _do_close(
        [StaleIssue(number=1, title="red", matched_pr=7)],
        dry_run=True,
        comment="close",
    )
    assert blocked.closed == []
    assert blocked.skipped_reason == "lander_holds"
    mixed = _do_close(
        [
            StaleIssue(number=1, title="red", matched_pr=7),
            StaleIssue(number=2, title="red", matched_pr=8),
        ],
        dry_run=True,
        comment="close",
    )
    assert [issue.matched_pr for issue in mixed.closed] == [8]

    claim(tmp_path, repo="acme/widgets", number=4, actor="auto_closer", head_sha="zzz")
    report = build_report([_pull(number=4, head_ref="feature/held")], repo="acme/widgets", state_dir=tmp_path)
    assert report.blocked_by_closer == [4]
    assert report.ready == []


def test_status_is_neutral_and_posting_is_refused():
    payload = advisory_status("prefix:cursor", [1, 2])
    assert payload["conclusion"] == "neutral"
    assert payload["blocking"] is False
    with pytest.raises(LanderRefused):
        GitHubForge(runner=lambda args: "[]").post_status("acme/widgets", "abc", "prefix:cursor", [1, 2])
    with pytest.raises(LanderRefused):
        refuse_remote_write("merge")
    with pytest.raises(LanderRefused):
        refuse_remote_write("enqueue")
    with pytest.raises(LanderRefused):
        refuse_remote_write("force_push")


def test_gitlab_is_a_documented_stub():
    with pytest.raises(ForgeUnavailable, match="[Mm]erge train"):
        GitLabForge().list_open("acme/widgets")


def test_a_failed_read_names_the_command_and_the_forge_error(monkeypatch):
    monkeypatch.setenv("GH_TOKEN", "super-secret-value")

    def boom(args, **kwargs):
        del args, kwargs

        class Done:
            returncode = 1
            stderr = (
                "GraphQL: Resource not accessible by integration "
                "(repository.pullRequest.statusCheckRollup) super-secret-value"
            )
            stdout = ""

        return Done()

    monkeypatch.setattr(
        "axiom.extensions.builtins.release.lander.adapters.subprocess.run",
        boom,
    )
    with pytest.raises(ForgeUnavailable, match="statusCheckRollup") as caught:
        default_gh(["pr", "view", "8", "--repo", "acme/widgets", "--json", "files,statusCheckRollup"])
    text = str(caught.value)
    assert "pr view" in text
    assert "files,statusCheckRollup" in text
    assert "super-secret-value" not in text


def test_a_gh_timeout_keeps_the_reason(monkeypatch):
    import subprocess

    def boom(args, **kwargs):
        del kwargs
        raise subprocess.TimeoutExpired(cmd=args, timeout=30)

    monkeypatch.setattr(
        "axiom.extensions.builtins.release.lander.adapters.subprocess.run",
        boom,
    )
    with pytest.raises(ForgeUnavailable, match="timed out") as caught:
        default_gh(["pr", "list", "--repo", "acme/widgets"])
    assert "pr list" in str(caught.value)


def test_github_parsing_does_not_put_a_token_on_the_command(monkeypatch):
    monkeypatch.setenv("GH_TOKEN", "super-secret-value")
    seen: list[list[str]] = []

    def runner(args: list[str]) -> str:
        seen.append(args)
        if args[0:2] == ["pr", "list"]:
            return json.dumps(
                [
                    {
                        "number": 8,
                        "title": "Fixes #3",
                        "headRefName": "cursor/eight",
                        "headRefOid": "deadbeef",
                        "author": {"login": "agent"},
                        "labels": [{"name": "session:s"}],
                        "updatedAt": "2026-10-25T12:00:00Z",
                        "createdAt": "2026-10-25T11:00:00Z",
                        "isDraft": False,
                        "mergeable": "MERGEABLE",
                        "url": "https://example.test/8",
                        "body": "",
                    }
                ]
            )
        return json.dumps(
            {
                "files": [{"path": "src/a.py"}],
                "statusCheckRollup": [
                    {"status": "COMPLETED", "conclusion": "SUCCESS", "name": "test"}
                ],
            }
        )

    pulls = GitHubForge(runner=runner).list_open("acme/widgets")
    assert pulls[0].checks == "passing"
    assert pulls[0].files == ("src/a.py",)
    assert "#3" in pulls[0].ticket_ids
    flat = " ".join(" ".join(args) for args in seen)
    assert "super-secret-value" not in flat
    failing = parse_pr(
        {"number": 1, "title": "t", "statusCheckRollup": [{"conclusion": "FAILURE", "status": "COMPLETED"}]}
    )
    assert failing.checks == "failing"


def test_a_land_mode_in_config_cannot_turn_merging_on():
    config = parse_config('mode = "land"\npost_status = true\n')
    assert config.mode == "warn"
    assert config.notes
    assert config.post_status is True


def test_human_paths_refuse_before_dry_run(tmp_path):
    assert human_route(["src/pricing/list.py"]) == ("pricing",)
    assert human_route(["migrations/001.sql"]) == ("data",)
    assert human_route(["tests/conftest.py"]) == ("test-setup",)
    assert human_route(["src/behavior/rule.py"]) == ("behavior",)
    assert human_route(["src/author.py"]) == ()
    decision = guarded_act(
        AgentAction(agent="rivet", op_class="github.pr.land", name="propose", candidates=[1]),
        do_one=lambda candidate: True,
        state_dir=tmp_path,
        dry_run=True,
        paths=["src/auth/session.py"],
    )
    assert decision.proceed is False
    assert decision.reason == "human_route:security"
    assert decision.would_proceed == []


def test_the_skill_reports_without_a_forge_call(tmp_path, capsys):
    result = land(
        {
            "repo": "acme/widgets",
            "format": "json",
            "pulls": [
                {
                    "number": 1,
                    "title": "change",
                    "head_sha": "abc",
                    "head_ref": "cursor/one",
                    "updated_at": T0.isoformat(),
                    "checks": "passing",
                    "mergeable": True,
                    "files": ["src/a.py"],
                }
            ],
            "samples": [14],
        },
        _ctx(tmp_path),
    )
    assert result.ok
    assert result.value["ready"][0]["number"] == 1
    assert result.value["minutes_saved"] == 0
    printed = json.loads(capsys.readouterr().out)
    assert printed["repo"] == "acme/widgets"
    assert "land" in build_parser().parse_args(["land", "--repo", "acme/widgets"]).verb


def test_heartbeat_with_no_repos_does_not_propose(tmp_path):
    assert heartbeat_propose(tmp_path) == {"ran": False, "reason": "no_repos", "mode": "warn"}


def test_the_lander_source_does_not_merge_or_force_push():
    assert "--force" not in push_command("origin", "refs/heads/topic")
    merged = merge_command(4, "merge", "abc")
    enqueued = merge_command(4, "enqueue", "abc")
    assert "--force" not in merged
    assert "--force" not in enqueued
    assert "--admin" not in merged
    assert "--admin" not in enqueued
