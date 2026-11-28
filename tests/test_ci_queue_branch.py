# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""A run on a queue branch takes main's full path and nothing that belongs to main (#2399).

RIVET's landing queue pushes the merged state of a batch to ``rivet-queue/<n>``. That run
must test everything main tests, and must never write what main's runs read as their own
(the blast-radius map, the test history), publish, or open a main incident.

GitHub documents that a push event runs "your workflow when you push a commit or tag" and
that ``branches`` filters are matched against the ref name, so a ``rivet-queue/**`` filter
fires for a branch named ``rivet-queue/x`` and not for a ref outside refs/heads.
"""

from __future__ import annotations

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
CI = ROOT / ".github" / "workflows" / "ci.yml"
QUEUE = "startsWith(github.ref, 'refs/heads/rivet-queue/')"
MAIN = "github.ref == 'refs/heads/main'"


def _wf() -> dict:
    return yaml.safe_load(CI.read_text())


def _steps():
    for job_id, job in _wf()["jobs"].items():
        for step in job.get("steps", []):
            yield job_id, step


def test_a_push_to_a_queue_branch_starts_ci_and_main_and_tags_still_do():
    push = _wf().get("on", _wf().get(True))["push"]
    assert push["branches"] == ["main", "rivet-queue/**"]
    assert push["tags"] == ["v*"]


def test_a_queue_run_is_full_through_the_one_full_decision():
    """Not a second set of conditions: the heavy jobs read `changes.outputs.full`, and every
    event that is not a pull request is full, a queue push included."""
    changes = _wf()["jobs"]["changes"]
    run = next(s for s in changes["steps"] if s.get("id") == "filter")["run"]
    head = run.split("exit 0", 1)[0]
    assert '!= "pull_request"' in head and 'echo "full=true"' in head
    for job_id in (
        "integration-tests",
        "migration-tests",
        "install-path-tests",
        "prerelease-wheel-check",
    ):
        cond = _wf()["jobs"][job_id]["if"]
        assert f"({QUEUE} && needs.changes.outputs.full == 'true')" in cond, job_id
        assert "changes" in _wf()["jobs"][job_id]["needs"], job_id
    shards = _wf()["jobs"]["unit-shards"]["if"]
    assert "needs.changes.outputs.full == 'true'" in shards and "pull_request" not in shards


def test_no_other_job_is_keyed_on_main_for_a_queue_run_to_miss():
    for job_id, job in _wf()["jobs"].items():
        cond = str(job.get("if", ""))
        # notify-failure is main-only by design; tag-already-green must NOT run on a queue
        # branch: a queue run is the first test of that tree and is never skipped.
        if MAIN in cond and job_id not in ("notify-failure", "tag-already-green"):
            assert QUEUE in cond, job_id


def test_no_queue_run_saves_what_main_reads_as_its_own():
    saves = [
        (j, s) for j, s in _steps() if str(s.get("uses", "")).startswith("actions/cache/save@")
    ]
    assert len(saves) >= 4
    for job_id, step in saves:
        assert f"!{QUEUE}" in str(step["if"]), (job_id, step.get("name"))


def test_a_queue_run_is_never_skipped_as_already_tested():
    gate = _wf()["jobs"]["tag-already-green"]["if"]
    assert "rivet-queue" not in gate


def test_a_queue_run_never_publishes_and_never_opens_a_main_incident():
    jobs = _wf()["jobs"]
    assert jobs["publish"]["if"] == "startsWith(github.ref, 'refs/tags/v')"
    incident = jobs["notify-failure"]["if"]
    assert "rivet-queue" not in incident
    assert MAIN in incident and "refs/tags/" in incident


def test_a_queue_branch_has_its_own_group_and_cancels_only_within_it():
    c = _wf()["concurrency"]
    assert "format('queue-{0}', github.ref)" in c["group"]
    assert QUEUE in c["cancel-in-progress"]
    # Main is matched before the queue case, so main's group is untouched.
    assert c["group"].index("'main'") < c["group"].index("queue-")


def test_nothing_main_only_is_left_that_a_queue_run_would_trip():
    """Any step or job conditioned on the main ref must say what a queue run does."""
    for job_id, step in _steps():
        cond = str(step.get("if", ""))
        if MAIN in cond:
            assert QUEUE in cond or "!" + QUEUE in cond, (job_id, step.get("name"))
    # The skip decision for a main push reads queue branches through the tested module.
    text = CI.read_text()
    assert "release.queue_tree" in text
    assert "GITHUB_STEP_SUMMARY" in text
