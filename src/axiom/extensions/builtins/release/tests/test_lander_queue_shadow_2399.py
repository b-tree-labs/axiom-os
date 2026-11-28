# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""#2399 slice 2: the read-only adapter, the batch plan, and shadow with no effect on verdicts."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from axiom.extensions.builtins.release.ci_health.client import CIClientError
from axiom.extensions.builtins.release.lander.config import LanderConfig, parse_config
from axiom.extensions.builtins.release.lander.loop import run_once
from axiom.extensions.builtins.release.lander.loop_guard import LanderRefused
from axiom.extensions.builtins.release.lander.model import OpenPull
from axiom.extensions.builtins.release.lander.queue import Candidate, bisect_plan, plan_batch
from axiom.extensions.builtins.release.lander.queue_adapter import (
    CiResult,
    FakeQueueReader,
    PullFacts,
    QueueReadError,
    QueueWriteRefused,
)
from axiom.extensions.builtins.release.lander.queue_github import GitHubQueueReader, checks_state
from axiom.infra.switchplan.ledger import FileLedger

REPO = "owner/name"
BASE = "b" * 40


def sha(n: int) -> str:
    return f"{n:040x}"


def pull_row(n: int, **over) -> dict:
    row = {
        "number": n,
        "head": {"sha": sha(n)},
        "base": {"ref": "main", "sha": BASE},
        "labels": [{"name": "x"}],
        "mergeable": True,
        "merge_commit_sha": sha(n + 1000),
    }
    return {**row, **over}


def checks_rows(*conclusions: str) -> dict:
    return {
        "check_runs": [
            {"status": "completed" if c else "in_progress", "conclusion": c or None}
            for c in conclusions
        ]
    }


def runner_for(routes: dict[str, object], seen: list | None = None):
    def run(args: list[str]) -> str:
        if seen is not None:
            seen.append(list(args))
        path = args[0].split("?")[0]
        if path not in routes:
            raise CIClientError(f"gh api {path}: 404")
        value = routes[path]
        return value if isinstance(value, str) else json.dumps(value)

    return run


def routes_for(*numbers: int, checks=("success",)) -> dict:
    routes: dict[str, object] = {
        f"repos/{REPO}/branches/main": {"commit": {"sha": BASE}},
        f"repos/{REPO}/pulls": [pull_row(n) for n in numbers],
    }
    for n in numbers:
        routes[f"repos/{REPO}/pulls/{n}"] = pull_row(n)
        routes[f"repos/{REPO}/commits/{sha(n)}/check-runs"] = checks_rows(*checks)
    return routes


# -- the read-only adapter -------------------------------------------------------------


def test_reader_lists_open_pulls_with_head_base_checks_labels_and_merged_state():
    seen: list = []
    reader = GitHubQueueReader(runner_for(routes_for(7, 8), seen))
    pulls = reader.open_pulls(REPO)
    assert [(p.number, p.head_sha, p.base_ref, p.checks) for p in pulls] == [
        (7, sha(7), "main", "passing"),
        (8, sha(8), "main", "passing"),
    ]
    assert pulls[0].merge_commit_sha == sha(1007)
    assert pulls[0].labels == ("x",)
    assert reader.base_sha(REPO, "main") == BASE
    # Every call is one plain path under repos/: no method, no field, no body.
    assert seen and all(len(a) == 1 and a[0].startswith("repos/") for a in seen)


def test_reader_ci_results_by_sha():
    runs = {
        "workflow_runs": [
            {
                "status": "completed",
                "conclusion": "success",
                "created_at": "2026-11-25T10:00:00Z",
                "run_started_at": "2026-11-25T10:00:00Z",
                "updated_at": "2026-11-25T10:30:00Z",
            },
            {
                "status": "completed",
                "conclusion": "failure",
                "created_at": "2026-11-25T10:00:00Z",
                "run_started_at": "2026-11-25T10:00:00Z",
                "updated_at": "2026-11-25T10:10:00Z",
            },
        ]
    }
    reader = GitHubQueueReader(runner_for({f"repos/{REPO}/actions/runs": runs}))
    got = reader.ci_results(REPO, sha(1))
    assert (got.state, got.runs, got.minutes) == ("failure", 2, 40.0)
    empty = GitHubQueueReader(runner_for({f"repos/{REPO}/actions/runs": {"workflow_runs": []}}))
    assert empty.ci_results(REPO, sha(1)).state == "none"


@pytest.mark.parametrize(
    "break_it",
    [
        lambda r: r.update(head={"sha": "abc"}),  # not a full sha
        lambda r: r.update(mergeable="yes"),  # not a bool
        lambda r: r.update(merge_commit_sha=None),  # mergeable but no merge commit
        lambda r: r.update(labels=None),
        lambda r: r.update(base=None),
        lambda r: r.pop("number"),
    ],
)
def test_reader_fails_closed_on_an_unreadable_value(break_it):
    row = pull_row(3)
    break_it(row)
    routes = routes_for(3)
    routes[f"repos/{REPO}/pulls/3"] = row
    with pytest.raises(QueueReadError):
        GitHubQueueReader(runner_for(routes)).pull_facts(REPO, 3)


def test_reader_fails_closed_on_errors_bad_json_and_unknown_check_state():
    reader = GitHubQueueReader(runner_for({}))
    with pytest.raises(QueueReadError):
        reader.base_sha(REPO, "main")
    with pytest.raises(QueueReadError):
        GitHubQueueReader(runner_for({f"repos/{REPO}/branches/main": "not json"})).base_sha(
            REPO, "main"
        )
    with pytest.raises(QueueReadError):
        checks_state([{"status": "completed", "conclusion": "mystery"}])
    # A conflicting pull request has no merged state, and says so rather than guessing.
    routes = routes_for(4)
    routes[f"repos/{REPO}/pulls/4"] = pull_row(4, mergeable=False, merge_commit_sha=sha(1))
    assert GitHubQueueReader(runner_for(routes)).pull_facts(REPO, 4).merge_commit_sha == ""
    assert checks_state([]) == "none"
    assert checks_state(checks_rows("success", "")["check_runs"]) == "pending"
    assert checks_state(checks_rows("success", "failure")["check_runs"]) == "failing"


def test_reader_never_writes():
    reader = GitHubQueueReader(runner_for(routes_for(1)))
    c = Candidate(1, sha(1), BASE)
    for call in (
        lambda: reader.build_candidate(c, ()),
        lambda: reader.land(c),
        lambda: reader.delete_branch("x"),
        lambda: reader.wait_for_checks("x"),
    ):
        with pytest.raises(QueueWriteRefused):
            call()
    with pytest.raises(QueueWriteRefused):
        reader._get("orgs/x/settings")


# -- the batch plan and the bisect plan ----------------------------------------------


def cands(*numbers: int) -> list[Candidate]:
    return [Candidate(n, sha(n), BASE) for n in numbers]


def test_plan_batch_is_bounded_ordered_named_and_not_created():
    plan = plan_batch(cands(5, 3, 9, 1, 2), base_sha=BASE, size=3)
    assert plan.members == (5, 3, 9)
    assert plan.ref == "rivet-queue/5"
    assert plan.to_dict()["created"] is False
    assert plan_batch(cands(5, 3, 9, 1, 2), base_sha=BASE, size=3) == plan


def test_plan_batch_empty_or_unknown_base_fails_closed():
    assert plan_batch(cands(1, 2), base_sha="", size=3).members == ()
    assert plan_batch(cands(1, 2), base_sha="", size=3).reason == "base_unknown"
    assert plan_batch([], base_sha=BASE, size=3).reason == "nothing_ready"
    with pytest.raises(ValueError):
        plan_batch(cands(1), base_sha=BASE, size=0)


def test_plan_batch_hotfix_leads():
    c = cands(1, 2, 3)
    c[2] = Candidate(3, sha(3), BASE, lane="hotfix")
    assert plan_batch(c, base_sha=BASE, size=2).members == (3, 1)


def test_bisect_plan_halves_and_bounds_the_cost():
    plan = bisect_plan([1, 2, 3, 4])
    assert plan["steps"][0] == {"test": [1, 2], "if_green": "land", "if_red": "narrow"}
    assert plan["worst_case_extra_runs"] == 3
    assert plan["removal_reason"] == "red_check"
    assert bisect_plan([7])["steps"] == [] and bisect_plan([7])["worst_case_extra_runs"] == 0
    assert bisect_plan([])["worst_case_extra_runs"] == 0


# -- shadow in the lander loop ---------------------------------------------------------


def ready_pull(n: int) -> OpenPull:
    return OpenPull(
        number=n,
        title=f"change {n}",
        head_sha=sha(n),
        head_ref=f"b{n}",
        checks="passing",
        mergeable=True,
        merge_state="CLEAN",
    )


def records(state_dir: Path) -> list:
    ledger = FileLedger(state_dir / "agents" / "rivet" / "lander-records")
    return [r for r in ledger.records() if r.kind == "release.lander.queue"]


def shadow_config(**over) -> LanderConfig:
    return replace(LanderConfig(), queue_mode="shadow", **over)


def reader_for(*numbers: int, **kw) -> FakeQueueReader:
    return FakeQueueReader(
        base=BASE,
        pulls={
            n: PullFacts(
                n,
                sha(n),
                "main",
                checks="passing",
                mergeable=True,
                merge_commit_sha=sha(n + 1000),
                base_sha=BASE,
            )
            for n in numbers
        },
        **kw,
    )


def test_shadow_records_batch_ref_bisect_and_what_the_lander_did(tmp_path):
    pulls = [ready_pull(n) for n in (1, 2, 3, 4, 5)]
    result = run_once(
        repo=REPO,
        state_dir=tmp_path,
        config=shadow_config(queue_batch_size=3),
        pulls=pulls,
        queue_adapter=reader_for(1, 2, 3, 4, 5),
    )
    (record,) = records(tmp_path)
    s = record.state
    ready = [item.number for item in result.report.ready]
    assert s["lander_ready"] == ready
    assert [a["pr"] for a in s["lander_did"] if a["action"] == "land"] == ready
    batch = s["batch"]
    assert batch["members"] == ready[:3]
    assert batch["ref"] == f"rivet-queue/{ready[0]}"
    assert batch["base_sha"] == BASE and batch["created"] is False
    assert s["if_red"]["members"] == ready[:3]
    assert s["if_red"]["steps"] and s["if_red"]["removal_reason"] == "red_check"
    assert (s["merged"], s["branches_created"], s["refs_created"], s["writes"]) == (False, 0, 0, 0)
    assert result.merged is False and result.enqueued is False


def test_shadow_does_not_change_the_verdict(tmp_path):
    pulls = [ready_pull(n) for n in (1, 2, 3)]
    off = run_once(repo=REPO, state_dir=tmp_path / "a", config=LanderConfig(), pulls=pulls)
    on = run_once(
        repo=REPO,
        state_dir=tmp_path / "b",
        config=shadow_config(),
        pulls=pulls,
        queue_adapter=reader_for(1, 2, 3),
    )
    broken = run_once(
        repo=REPO,
        state_dir=tmp_path / "c",
        config=shadow_config(),
        pulls=pulls,
        queue_adapter=object(),  # raises inside shadow; the lander still answers the same
    )
    assert off.report.to_dict() == on.report.to_dict() == broken.report.to_dict()
    log = (tmp_path / "c" / "agents" / "rivet" / "lander.jsonl").read_text()
    assert "queue_shadow_error" in log
    assert records(tmp_path / "c") == []


def test_shadow_fails_closed_on_unknown_base_and_unreadable_pulls(tmp_path):
    pulls = [ready_pull(n) for n in (1, 2)]
    run_once(
        repo=REPO,
        state_dir=tmp_path / "a",
        config=shadow_config(),
        pulls=pulls,
        queue_adapter=reader_for(1, 2, base_unreadable=True),
    )
    (rec,) = records(tmp_path / "a")
    assert rec.state["batch"]["members"] == [] and "base_unreadable" in rec.state["batch"]["reason"]
    # No reader and injected pulls: nothing is read, nothing is guessed.
    run_once(repo=REPO, state_dir=tmp_path / "b", config=shadow_config(), pulls=pulls)
    (rec,) = records(tmp_path / "b")
    assert rec.state["batch"]["members"] == [] and rec.state["batch"]["reason"] == "no_reader"
    # An empty base sha is unknown, the same way.
    run_once(
        repo=REPO,
        state_dir=tmp_path / "c",
        config=shadow_config(),
        pulls=pulls,
        queue_adapter=reader_for(1, 2).__class__(base="", pulls=reader_for(1, 2).pulls),
    )
    (rec,) = records(tmp_path / "c")
    assert rec.state["batch"]["members"] == []
    # One unreadable pull request is dropped with a code; the other is still planned.
    run_once(
        repo=REPO,
        state_dir=tmp_path / "d",
        config=shadow_config(),
        pulls=pulls,
        queue_adapter=reader_for(1, 2, unreadable={1}),
    )
    (rec,) = records(tmp_path / "d")
    assert rec.state["excluded"] == [{"pr": 1, "reason": "unreadable"}]
    assert rec.state["batch"]["members"] == [2]


def test_shadow_excludes_conflict_moved_head_and_red_with_codes(tmp_path):
    pulls = [ready_pull(n) for n in (1, 2, 3, 4)]
    reader = reader_for(1, 2, 3, 4)
    reader.pulls[1] = replace(reader.pulls[1], mergeable=False, merge_commit_sha="")
    reader.pulls[2] = replace(reader.pulls[2], head_sha=sha(99))
    reader.pulls[3] = replace(reader.pulls[3], checks="failing")
    run_once(
        repo=REPO, state_dir=tmp_path, config=shadow_config(), pulls=pulls, queue_adapter=reader
    )
    (rec,) = records(tmp_path)
    assert {e["pr"]: e["reason"] for e in rec.state["excluded"]} == {
        1: "conflict",
        2: "stale_base",
        3: "red_check",
    }
    assert rec.state["batch"]["members"] == [4]


def test_shadow_pause_is_the_kill_switch_and_the_release_lock(tmp_path):
    from axiom.extensions.builtins.release.lander import queue_shadow

    pulls = [ready_pull(1)]
    cfg = shadow_config()
    s = queue_shadow.shadow_queue_record(
        repo=REPO,
        state_dir=tmp_path,
        config=cfg,
        ready=pulls,
        reader=reader_for(1),
        environ={cfg.kill_switch_env: "1"},
    )
    assert s["batch"]["blocked_by"] == "owner_pause" and s["batch"]["would_start"] is False
    assert s["held_reasons"] == ["owner_pause"]
    s = queue_shadow.shadow_queue_record(
        repo=REPO,
        state_dir=tmp_path,
        config=cfg,
        ready=pulls,
        reader=reader_for(1),
        environ={},
    )
    assert s["batch"]["would_start"] is True


def test_batch_size_setting_defaults_and_validates():
    assert LanderConfig().queue_batch_size == 4
    assert parse_config("[queue]\nbatch_size = 6\n").queue_batch_size == 6
    for bad in ("0", "true", '"x"'):
        with pytest.raises(LanderRefused):
            parse_config(f"[queue]\nbatch_size = {bad}\n")


def test_ci_result_is_a_value():
    assert CiResult("s", "none").runs == 0
