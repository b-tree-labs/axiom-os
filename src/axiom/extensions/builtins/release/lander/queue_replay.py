# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Replay merged history through the landing queue's cost model (#2399, slice 2).

Read-only: pull requests and workflow runs come from ``gh api`` GET reads, the
simulation is pure, and nothing is written but what the caller prints. Run it as

    python -m axiom.extensions.builtins.release.lander.queue_replay OWNER/NAME --limit 100

It answers one question with real numbers: if a queue had tested the merged state before
landing, how many main runs would a batch have replaced, how long would a pull request
wait to land, and how many red main runs would a pre-landing test have caught. Its limits
are part of the output (``LIMITS``), not a footnote.
"""

from __future__ import annotations

import argparse
import json
import statistics
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from axiom.extensions.builtins.release.ci_health.client import _default_runner

WORKFLOW = "ci.yml"
LIMITS = (
    "Ready time is the finish of the pull request's last green run of the head, or the merge "
    "time when it had none; the real moment the owner or lander would have enqueued it is not recorded.",
    "One suite duration (the median of completed main runs) stands for every batch; a batch of "
    "several pull requests is assumed to cost one run, which is the optimistic case for batching.",
    "A red main run is attributed to the pull request whose merge commit it ran on. Runs that were "
    "cancelled, or that covered several merges, cannot be attributed and are counted apart.",
    "Whether a red was a real defect or a flake is not known from run metadata: a flaky red would "
    "also block a queue run, so 'caught' is an upper bound for defects and a cost for flakes.",
    "Wall-clock minutes are measured; runner-minutes per suite are sampled from a few recent jobs.",
    "Pull-request runs skip the sharded unit suite by design, so a green pull request says little "
    "about the merged state: the queue's value is concentrated there.",
)


def _t(value: object) -> datetime:
    return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(UTC)


@dataclass(frozen=True)
class MergedPull:
    number: int
    title: str
    head_sha: str
    merge_sha: str
    merged_at: datetime


@dataclass(frozen=True)
class Run:
    id: int
    event: str
    branch: str
    sha: str
    status: str
    conclusion: str
    started: datetime
    finished: datetime
    attempt: int = 1

    @property
    def minutes(self) -> float:
        return max((self.finished - self.started).total_seconds() / 60.0, 0.0)


def parse_pulls(rows: Iterable[dict]) -> list[MergedPull]:
    out = [
        MergedPull(
            number=int(r["number"]),
            title=str(r.get("title") or ""),
            head_sha=str((r.get("head") or {}).get("sha") or ""),
            merge_sha=str(r.get("merge_commit_sha") or ""),
            merged_at=_t(r["merged_at"]),
        )
        for r in rows
        if isinstance(r, dict) and r.get("merged_at")
    ]
    return sorted(out, key=lambda p: p.merged_at)


def parse_runs(rows: Iterable[dict]) -> list[Run]:
    out = []
    for r in rows:
        if not isinstance(r, dict) or not r.get("id"):
            continue
        started = _t(r.get("run_started_at") or r["created_at"])
        out.append(
            Run(
                id=int(r["id"]),
                event=str(r.get("event") or ""),
                branch=str(r.get("head_branch") or ""),
                sha=str(r.get("head_sha") or ""),
                status=str(r.get("status") or ""),
                conclusion=str(r.get("conclusion") or ""),
                started=started,
                finished=_t(r.get("updated_at") or r["created_at"]),
                attempt=int(r.get("run_attempt") or 1),
            )
        )
    return out


def _pct(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(round(q * (len(ordered) - 1))))]


def ready_times(pulls: list[MergedPull], runs: list[Run]) -> tuple[dict[int, datetime], set[int]]:
    """When each pull request went green, and which had no green run of their head."""
    ready: dict[int, datetime] = {}
    ungreen: set[int] = set()
    for pull in pulls:
        greens = [
            r
            for r in runs
            if r.event == "pull_request"
            and r.sha == pull.head_sha
            and r.conclusion == "success"
            and r.started < pull.merged_at
        ]
        if greens:
            ready[pull.number] = min(max(r.finished for r in greens), pull.merged_at)
        else:
            ready[pull.number] = pull.merged_at
            ungreen.add(pull.number)
    return ready, ungreen


def simulate_queue(ready: dict[int, datetime], *, batch_size: int, suite_minutes: float) -> dict:
    """One merged-state test at a time; a batch takes up to ``batch_size`` that are ready.

    A pull request waits for the test in flight, then joins the next batch. The test takes
    ``suite_minutes`` whatever the batch size (the optimistic case).
    """
    pending = sorted(ready.items(), key=lambda kv: (kv[1], kv[0]))
    free = datetime.min.replace(tzinfo=UTC)
    waits: dict[int, float] = {}
    batches: list[list[int]] = []
    i = 0
    while i < len(pending):
        start = max(free, pending[i][1])
        group = [kv for kv in pending[i:] if kv[1] <= start][:batch_size]
        i += len(group)
        land = start + timedelta(minutes=suite_minutes)
        batches.append([n for n, _ in group])
        for n, at in group:
            waits[n] = (land - at).total_seconds() / 60.0
        free = land
    return {"batches": batches, "wait_minutes": waits}


def main_runs(runs: list[Run], since: datetime | None = None) -> list[Run]:
    return sorted(
        (
            r
            for r in runs
            if r.event == "push" and r.branch == "main" and (since is None or r.started >= since)
        ),
        key=lambda r: r.started,
    )


def verified_minutes(pulls: list[MergedPull], mains: list[Run]) -> dict[int, float]:
    """Today: minutes from merge until a completed main run covers the merge commit."""
    done = [r for r in mains if r.status == "completed" and r.conclusion in {"success", "failure"}]
    out: dict[int, float] = {}
    for pull in pulls:
        own = [r for r in done if r.sha == pull.merge_sha]
        later = [r for r in done if r.started >= pull.merged_at]
        cover = own[-1:] or sorted(later, key=lambda r: r.finished)[:1]
        if cover:
            out[pull.number] = max((cover[0].finished - pull.merged_at).total_seconds() / 60.0, 0.0)
    return out


def classify_reds(pulls: list[MergedPull], runs: list[Run], ungreen: set[int]) -> dict:
    by_merge = {p.merge_sha: p for p in pulls}
    window_start = pulls[0].merged_at if pulls else datetime.min.replace(tzinfo=UTC)
    reds = [
        r
        for r in main_runs(runs)
        if r.status == "completed" and r.conclusion == "failure" and r.started >= window_start
    ]
    catchable: list[int] = []
    merged_red: list[int] = []
    unattributed = 0
    for red in reds:
        pull = by_merge.get(red.sha)
        if pull is None:
            unattributed += 1
        elif pull.number in ungreen:
            merged_red.append(pull.number)
        else:
            catchable.append(pull.number)
    return {
        "red_main_runs": len(reds),
        "merged_state_only": sorted(set(catchable)),
        "merged_without_green_pr_run": sorted(set(merged_red)),
        "unattributed": unattributed,
    }


def replay(
    pulls: list[MergedPull],
    runs: list[Run],
    *,
    batch_sizes: tuple[int, ...] = (1, 2, 4, 8),
    runner_minutes_per_suite: float = 0.0,
) -> dict:
    window_start = pulls[0].merged_at
    mains = main_runs(runs, window_start)
    completed = [
        r for r in mains if r.status == "completed" and r.conclusion in {"success", "failure"}
    ]
    suite = statistics.median(r.minutes for r in completed) if completed else 0.0
    ready, ungreen = ready_times(pulls, runs)
    today_land = [(p.merged_at - ready[p.number]).total_seconds() / 60.0 for p in pulls]
    verified = list(verified_minutes(pulls, mains).values())
    span_h = (
        ((pulls[-1].merged_at - pulls[0].merged_at).total_seconds() / 3600.0)
        if len(pulls) > 1
        else 0.0
    )
    table = []
    for size in batch_sizes:
        sim = simulate_queue(ready, batch_size=size, suite_minutes=suite)
        waits = list(sim["wait_minutes"].values())
        n = len(sim["batches"])
        table.append(
            {
                "batch_size": size,
                "batches": n,
                "mean_batch": round(len(pulls) / n, 2) if n else 0.0,
                "queue_runs": n,
                "main_runs_today": len(mains),
                "runs_saved": len(mains) - n,
                "median_to_land_min": round(statistics.median(waits), 1) if waits else 0.0,
                "p90_to_land_min": round(_pct(waits, 0.9), 1),
            }
        )
    return {
        "merged_pulls": len(pulls),
        "span_hours": round(span_h, 1),
        "suite_minutes_median": round(suite, 1),
        "runner_minutes_per_suite": runner_minutes_per_suite,
        "main_runs_today": len(mains),
        "main_runs_cancelled": sum(1 for r in mains if r.conclusion == "cancelled"),
        "main_runs_red": sum(1 for r in mains if r.conclusion == "failure"),
        "main_wall_minutes_today": round(sum(r.minutes for r in mains), 1),
        "today_green_to_merge_median_min": round(statistics.median(today_land), 1)
        if today_land
        else 0.0,
        "today_merge_to_verified_median_min": round(statistics.median(verified), 1)
        if verified
        else 0.0,
        "today_merge_to_verified_p90_min": round(_pct(verified, 0.9), 1),
        "merged_without_green_pr_run": len(ungreen),
        "reds": classify_reds(pulls, runs, ungreen),
        "table": table,
        "limits": list(LIMITS),
    }


# -- reading ---------------------------------------------------------------------------


Getter = Callable[[str], Any]


def _default_get(path: str) -> Any:
    return json.loads(_default_runner([path]) or "null")


def fetch_merged_pulls(get: Getter, repo: str, limit: int) -> list[dict]:
    rows: list[dict] = []
    for page in range(1, 21):
        data = get(
            f"repos/{repo}/pulls?state=closed&base=main&sort=updated&direction=desc&per_page=100&page={page}"
        )
        if not isinstance(data, list) or not data:
            break
        rows.extend(r for r in data if isinstance(r, dict) and r.get("merged_at"))
        if len(rows) >= limit or len(data) < 100:
            break
    rows.sort(key=lambda r: r["merged_at"], reverse=True)
    return rows[:limit]


def fetch_runs(get: Getter, repo: str, since: datetime) -> list[dict]:
    rows: list[dict] = []
    day = (since - timedelta(days=1)).strftime("%Y-%m-%d")
    for page in range(1, 41):
        data = get(
            f"repos/{repo}/actions/workflows/{WORKFLOW}/runs?per_page=100&page={page}&created=%3E%3D{day}"
        )
        batch = data.get("workflow_runs") if isinstance(data, dict) else None
        if not batch:
            break
        rows.extend(r for r in batch if isinstance(r, dict))
        if len(batch) < 100:
            break
    return rows


def runner_minutes(get: Getter, repo: str, runs: list[Run], sample: int = 4) -> float:
    """Job-minutes of a few recent successful main runs: what one suite costs in runners."""
    picks = [r for r in reversed(main_runs(runs)) if r.conclusion == "success"][:sample]
    totals = []
    for run in picks:
        try:
            data = get(f"repos/{repo}/actions/runs/{run.id}/jobs?per_page=100")
        except Exception:  # noqa: BLE001 - a sample that cannot be read is left out
            continue
        jobs = data.get("jobs") if isinstance(data, dict) else None
        total = 0.0
        for job in jobs or []:
            if (
                job.get("conclusion") == "success"
                and job.get("started_at")
                and job.get("completed_at")
            ):
                total += (_t(job["completed_at"]) - _t(job["started_at"])).total_seconds() / 60.0
        if total:
            totals.append(total)
    return round(statistics.median(totals), 1) if totals else 0.0


def case_report(get: Getter, repo: str, number: int, closed: list[dict] | None = None) -> dict:
    """One named pull request: its own CI at its head, and main's run on its merge commit."""
    pull = get(f"repos/{repo}/pulls/{number}")
    head = str((pull.get("head") or {}).get("sha") or "")
    merge = str(pull.get("merge_commit_sha") or "")

    def runs_for(sha: str, event: str) -> list[dict]:
        data = get(
            f"repos/{repo}/actions/workflows/{WORKFLOW}/runs?head_sha={sha}&event={event}&per_page=20"
        )
        return [r for r in (data or {}).get("workflow_runs", []) if isinstance(r, dict)]

    def failed_jobs(run_id: int) -> list[str]:
        data = get(f"repos/{repo}/actions/runs/{run_id}/jobs?per_page=100")
        return sorted(
            {j["name"] for j in (data or {}).get("jobs", []) if j.get("conclusion") == "failure"}
        )

    def job_names(run_id: int) -> set[str]:
        data = get(f"repos/{repo}/actions/runs/{run_id}/jobs?per_page=100")
        return {
            j["name"]
            for j in (data or {}).get("jobs", [])
            if j.get("conclusion") in {"success", "failure"}
        }

    merged_at = str(pull.get("merged_at") or "")
    # A run created after the merge (a closed or labelled event) is not the pull request's test.
    pr_runs = sorted(
        (r for r in runs_for(head, "pull_request") if not merged_at or r["created_at"] < merged_at),
        key=lambda r: r["created_at"],
    )
    main = sorted(runs_for(merge, "push"), key=lambda r: r["created_at"]) if merge else []
    out: dict[str, Any] = {
        "pr": number,
        "title": str(pull.get("title") or ""),
        "merged_at": str(pull.get("merged_at") or ""),
        "pr_run": (pr_runs[-1]["conclusion"] if pr_runs else "none"),
        "main_run": (main[-1]["conclusion"] if main else "none"),
        "main_failed_jobs": [],
        "failed_jobs_that_ran_on_the_pr": [],
    }
    done = [r for r in main if r.get("conclusion") in {"success", "failure"}]
    covering = "own"
    if not done and merged_at:
        # Its own main run never reported (cancelled or superseded): the next one that did covers it.
        day = merged_at[:10]
        data = get(
            f"repos/{repo}/actions/workflows/{WORKFLOW}/runs?branch=main&event=push&per_page=100&created=%3E%3D{day}"
        )
        later = sorted(
            (
                r
                for r in (data or {}).get("workflow_runs", [])
                if r.get("conclusion") in {"success", "failure"} and r["created_at"] >= merged_at
            ),
            key=lambda r: r["created_at"],
        )
        done, covering = later[:1], "next_run"
    out["covered_by"] = covering if done else "none"
    out["main_run"] = done[-1]["conclusion"] if done else out["main_run"]
    out["reverted_by"] = sorted(
        int(r["number"])
        for r in (closed or [])
        if any(word in str(r.get("title") or "").lower() for word in ("revert", "reland"))
        and (f"#{number}" in str(r.get("title") or "") or f"#{number}" in str(r.get("body") or ""))
    )
    red = [r for r in done if r.get("conclusion") == "failure"]
    if red:
        failed = failed_jobs(int(red[-1]["id"]))
        out["main_failed_jobs"] = failed
        if pr_runs:
            ran = job_names(int(pr_runs[-1]["id"]))
            out["failed_jobs_that_ran_on_the_pr"] = [j for j in failed if j in ran]
    return out


def render(result: dict, cases: list[dict]) -> str:
    r = result
    lines = [
        f"Replay of {r['merged_pulls']} merged pull requests over {r['span_hours']} h.",
        f"Today: {r['main_runs_today']} main runs ({r['main_runs_cancelled']} cancelled, "
        f"{r['main_runs_red']} red), {r['main_wall_minutes_today']} wall-minutes; "
        f"suite median {r['suite_minutes_median']} min, about {r['runner_minutes_per_suite']} runner-minutes.",
        f"Today green-to-merge median {r['today_green_to_merge_median_min']} min; "
        f"merge-to-verified median {r['today_merge_to_verified_median_min']} min "
        f"(p90 {r['today_merge_to_verified_p90_min']}).",
        "",
        "| batch | batches | mean size | queue runs | main runs today | runs saved | median to land | p90 to land |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for row in r["table"]:
        lines.append(
            f"| {row['batch_size']} | {row['batches']} | {row['mean_batch']} | {row['queue_runs']} | "
            f"{row['main_runs_today']} | {row['runs_saved']} | {row['median_to_land_min']} | {row['p90_to_land_min']} |"
        )
    reds = r["reds"]
    lines += [
        "",
        f"Red main runs {reds['red_main_runs']}: merged-state-only (PR run was green) "
        f"{len(reds['merged_state_only'])} {reds['merged_state_only']}; merged without a green PR run "
        f"{len(reds['merged_without_green_pr_run'])}; unattributed {reds['unattributed']}.",
    ]
    for case in cases:
        lines.append(
            f"#{case['pr']}: PR run {case['pr_run']}, main run ({case['covered_by']}) {case['main_run']}, "
            f"reverted by {case['reverted_by']}, "
            f"failed on main {case['main_failed_jobs']}, of those also run on the PR "
            f"{case['failed_jobs_that_ran_on_the_pr']}"
        )
    lines += ["", "Limits:", *[f"- {item}" for item in r["limits"]]]
    return "\n".join(lines)


def main(argv: list[str] | None = None, *, get: Getter | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("repo")
    parser.add_argument("--limit", type=int, default=100)
    parser.add_argument("--case", type=int, action="append", default=[])
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    read = get or _default_get
    pulls = parse_pulls(fetch_merged_pulls(read, args.repo, args.limit))
    if not pulls:
        print("no merged pull requests")
        return 1
    runs = parse_runs(fetch_runs(read, args.repo, pulls[0].merged_at - timedelta(days=2)))
    result = replay(pulls, runs, runner_minutes_per_suite=runner_minutes(read, args.repo, runs))
    closed = fetch_merged_pulls(read, args.repo, 300)
    cases = [case_report(read, args.repo, n, closed) for n in args.case]
    print(
        json.dumps({"result": result, "cases": cases}, indent=2)
        if args.json
        else render(result, cases)
    )
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
