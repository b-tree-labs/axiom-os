# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""#2399 slice 2: the replay tool, on a small hand-made history with known answers."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from axiom.extensions.builtins.release.lander import queue_replay as rp

T0 = datetime(2026, 11, 20, 12, 0, tzinfo=UTC)


def at(minutes: float) -> str:
    return (T0 + timedelta(minutes=minutes)).strftime("%Y-%m-%dT%H:%M:%SZ")


def pull_row(n: int, merged: float) -> dict:
    return {
        "number": n,
        "title": f"p{n}",
        "head": {"sha": f"h{n}"},
        "merge_commit_sha": f"m{n}",
        "merged_at": at(merged),
        "body": "",
    }


def run_row(
    i: int, event: str, sha: str, conclusion: str, start: float, end: float, branch: str = "main"
) -> dict:
    return {
        "id": i,
        "event": event,
        "head_branch": branch,
        "head_sha": sha,
        "status": "completed",
        "conclusion": conclusion,
        "created_at": at(start),
        "run_started_at": at(start),
        "updated_at": at(end),
    }


def history():
    pulls = rp.parse_pulls([pull_row(1, 10), pull_row(2, 12), pull_row(3, 14), pull_row(4, 100)])
    runs = rp.parse_runs(
        [
            run_row(1, "pull_request", "h1", "success", 0, 5, "b1"),
            run_row(2, "pull_request", "h2", "success", 0, 6, "b2"),
            run_row(3, "pull_request", "h3", "success", 0, 7, "b3"),
            run_row(4, "pull_request", "h4", "success", 80, 90, "b4"),
            run_row(10, "push", "m1", "success", 10, 40),  # 30 min
            run_row(11, "push", "m2", "cancelled", 12, 15),
            run_row(12, "push", "m3", "failure", 14, 44),  # red on merged state; PR was green
            run_row(13, "push", "m4", "success", 100, 130),
        ]
    )
    return pulls, runs


def test_replay_counts_main_runs_batches_and_reds():
    pulls, runs = history()
    out = rp.replay(pulls, runs, batch_sizes=(1, 4))
    assert out["merged_pulls"] == 4
    assert (
        out["main_runs_today"] == 4
        and out["main_runs_cancelled"] == 1
        and out["main_runs_red"] == 1
    )
    assert out["suite_minutes_median"] == 30.0
    assert out["reds"]["merged_state_only"] == [3]
    assert out["reds"]["unattributed"] == 0
    one, four = out["table"]
    # Ready at 5, 6, 7, 90. One at a time: four runs. With a batch of four: PR 1 starts
    # alone at 5; 2 and 3 wait for it and share the second run; 4 arrives alone.
    assert one["batches"] == 4
    assert four["batches"] == 3 and four["runs_saved"] == 4 - 3
    assert out["limits"]


def test_simulate_queue_waits_for_the_test_in_flight():
    ready = {1: T0, 2: T0 + timedelta(minutes=1), 3: T0 + timedelta(minutes=2)}
    sim = rp.simulate_queue(ready, batch_size=4, suite_minutes=10)
    assert sim["batches"] == [[1], [2, 3]]
    assert sim["wait_minutes"] == {1: 10.0, 2: 19.0, 3: 18.0}
    assert rp.simulate_queue(ready, batch_size=1, suite_minutes=10)["batches"] == [[1], [2], [3]]


def test_a_pull_with_no_green_run_before_merge_is_flagged_not_dropped():
    pulls = rp.parse_pulls([pull_row(1, 10)])
    runs = rp.parse_runs(
        [
            run_row(1, "pull_request", "h1", "success", 11, 15, "b1"),  # after merge
            run_row(2, "push", "m1", "failure", 10, 40),
        ]
    )
    ready, ungreen = rp.ready_times(pulls, runs)
    assert ungreen == {1} and ready[1] == pulls[0].merged_at
    assert rp.classify_reds(pulls, runs, ungreen)["merged_without_green_pr_run"] == [1]


def test_merge_to_verified_uses_the_next_completed_run_when_its_own_was_cancelled():
    pulls, runs = history()
    got = rp.verified_minutes(pulls, rp.main_runs(runs))
    assert got[1] == 30.0
    assert got[2] == 44 - 12  # m2 was cancelled: the run that finished covers it


def test_case_report_names_failed_jobs_and_reverts():
    calls = {
        "repos/o/r/pulls/3": {
            "title": "p3",
            "merged_at": at(14),
            "head": {"sha": "h3"},
            "merge_commit_sha": "m3",
        },
        "repos/o/r/actions/workflows/ci.yml/runs": None,
    }

    def get(path: str):
        base = path.split("?")[0]
        if base in calls and calls[base] is not None:
            return calls[base]
        if "event=pull_request" in path:
            return {
                "workflow_runs": [
                    {"id": 3, "created_at": at(0), "conclusion": "success"},
                    {"id": 99, "created_at": at(15), "conclusion": "failure"},
                ]
            }
        if "event=push" in path:
            return {"workflow_runs": [{"id": 12, "created_at": at(14), "conclusion": "failure"}]}
        if base.endswith("/runs/12/jobs"):
            return {
                "jobs": [
                    {"name": "Unit", "conclusion": "failure"},
                    {"name": "Migration", "conclusion": "failure"},
                ]
            }
        if base.endswith("/runs/3/jobs"):
            return {"jobs": [{"name": "Unit", "conclusion": "success"}]}
        raise AssertionError(path)

    closed = [
        {"number": 9, "title": "reland: p3", "body": "relands #3"},
        {"number": 8, "title": "unrelated", "body": "mentions #3"},
    ]
    case = rp.case_report(get, "o/r", 3, closed)
    assert case["pr_run"] == "success"  # the run created after the merge is not the test
    assert case["main_run"] == "failure"
    assert case["main_failed_jobs"] == ["Migration", "Unit"]
    assert case["failed_jobs_that_ran_on_the_pr"] == ["Unit"]
    assert case["reverted_by"] == [9]


def test_render_has_the_table_and_the_limits():
    pulls, runs = history()
    text = rp.render(rp.replay(pulls, runs, batch_sizes=(1, 4)), [])
    assert "| batch | batches |" in text and "Limits:" in text
