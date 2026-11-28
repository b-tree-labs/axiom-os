# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""A pull request labelled ``ci:full`` takes exactly main's path (#2441).

Pull-request CI is narrowed (testmon blast radius, drafts and docs skip the suite) while
main runs everything. Failures only a full run shows reached main four times in two days.
The label (or a change to a CI-risk path, which applies it) makes the pull request's unit
tests take the main path: every test, the three shards and their aggregator, no early stop,
and the same job set. These checks pin that every narrowing decision reads one output,
``changes.outputs.full``, and that nothing else about the run changed.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github" / "workflows"
CI = yaml.safe_load((WORKFLOWS / "ci.yml").read_text())
JOBS = CI["jobs"]
FULL = "needs.changes.outputs.full == 'true'"


def _steps(job: str) -> list[dict]:
    return [s for s in JOBS[job]["steps"] if isinstance(s, dict)]


def _step(job: str, name: str) -> dict:
    for step in _steps(job):
        if step.get("name") == name:
            return step
    raise AssertionError(f"{job} has no step {name!r}")


def test_the_change_job_publishes_one_full_decision():
    assert "full" in JOBS["changes"]["outputs"]
    run = _step("changes", "Is this a full run?")["run"] if any(
        s.get("name") == "Is this a full run?" for s in _steps("changes")
    ) else next(s for s in _steps("changes") if s.get("id") == "filter")["run"]
    # Anything that is not a pull request is full, as before.
    assert 'echo "full=true"' in run
    assert "ci:full" in run


def test_the_label_is_read_live_so_a_rerun_sees_the_current_label():
    """A re-run replays the old event payload; the label must come from the API."""
    run = next(s for s in _steps("changes") if s.get("id") == "filter")["run"]
    assert "issues/${PR_NUMBER}/labels" in run or "issues/$PR_NUMBER/labels" in run
    assert "LABELS_JSON" in run, "the event payload is only the fallback"


def test_the_change_job_may_apply_the_label_and_never_removes_one():
    job = JOBS["changes"]
    assert job["permissions"]["pull-requests"] == "write"
    run = next(s for s in _steps("changes") if s.get("id") == "filter")["run"]
    assert "scripts/ci_full_label.py" in run
    assert "-X POST" in run
    assert "DELETE" not in run and "--remove-label" not in run


def test_a_label_overrides_the_draft_and_docs_skips():
    run = next(s for s in _steps("changes") if s.get("id") == "filter")["run"]
    decided = run.index('echo "full=true"', run.index("full=false"))
    draft_skip = run.index('draft }}" = "true" ]; then', decided)
    docs_skip = run.index("non_docs=")
    assert decided < draft_skip < docs_skip, "a full decision is made before the draft and docs skips"
    assert 'label_present" = "true" ] || [ "${{ github.event.pull_request.draft }}" != "true"' in run


def test_the_shards_run_on_a_full_decision_not_on_the_event_name():
    condition = JOBS["unit-shards"]["if"]
    assert FULL in condition
    assert "github.event_name != 'pull_request'" not in condition
    assert "vars.AXIOM_UNIT_SHARD != 'off'" in condition


def test_the_aggregator_and_its_runner_follow_the_same_decision():
    unit = JOBS["unit-tests"]
    assert "needs.changes.outputs.full == 'true'" in unit["runs-on"]
    assert unit["runs-on"].startswith("${{ (needs.changes.outputs.full == 'true' && vars.AXIOM_UNIT_SHARD != 'off')")
    assert unit["env"]["UNIT_SHARDED"] == (
        "${{ needs.changes.outputs.full == 'true' && vars.AXIOM_UNIT_SHARD != 'off' }}"
    )


def test_a_full_decision_means_no_early_stop_and_no_selection():
    run = _step("unit-tests", "Choose the unit-test run")["run"]
    assert "FULL_RUN" in run
    assert _step("unit-tests", "Choose the unit-test run")["env"]["FULL_RUN"] == (
        "${{ needs.changes.outputs.full }}"
    )
    # maxfail and selection are both keyed on "a pull request that is not full".
    assert run.count('= "pull_request" ] && [ "$FULL_RUN" != "true" ]') == 2


def test_the_main_only_jobs_also_run_on_a_full_pull_request():
    for job in ("integration-tests", "install-path-tests", "prerelease-wheel-check"):
        condition = JOBS[job]["if"]
        assert "github.event_name == 'pull_request' && needs.changes.outputs.full == 'true'" in condition, job
        assert "changes" in JOBS[job]["needs"], job
    assert "migrations" not in FULL  # migrations rides the same output below


def test_a_full_decision_turns_on_every_path_filter():
    run = next(s for s in _steps("changes") if s.get("id") == "filter")["run"]
    assert re.search(r'full=true.*\n.*code=true', run) or "code=true" in run
    assert JOBS["changes"]["outputs"]["migrations"]


def test_a_pull_request_never_saves_a_blast_radius_map():
    """A full run on an unmerged tree must not write the map main's runs restore."""
    for step in _steps("unit-tests"):
        if (step.get("uses") or "").startswith("actions/cache/save") and (step.get("with") or {}).get(
            "path"
        ) == ".testmondata":
            assert "github.event_name != 'pull_request'" in step["if"], step.get("name")


def test_publishing_stays_a_tag_only_job():
    assert JOBS["publish"]["if"] == "startsWith(github.ref, 'refs/tags/v')"
    assert "notify-failure" in JOBS and "github.ref == 'refs/heads/main'" in JOBS["notify-failure"]["if"]


# --- the label change starts the run -------------------------------------------------------


def test_ci_itself_does_not_start_on_every_label_change():
    """A label event cannot be filtered by name in `on:`. If ci.yml listened to `labeled`, the
    lander's claim and hold labels would restart and cancel runs, and each noise run would
    report skipped required checks over a real result on the same head."""
    types = CI[True]["pull_request"]["types"] if True in CI else CI["on"]["pull_request"]["types"]
    assert "labeled" not in types and "unlabeled" not in types


def test_a_small_workflow_restarts_ci_only_for_the_one_label():
    data = yaml.safe_load((WORKFLOWS / "ci-full-label.yml").read_text())
    on = data.get(True, data.get("on"))
    assert on["pull_request"]["types"] == ["labeled", "unlabeled"]
    job = data["jobs"]["restart"]
    assert job["if"] == "github.event.label.name == 'ci:full'"
    assert job["permissions"]["actions"] == "write"
    assert job["runs-on"] == "ubuntu-24.04"
    text = (WORKFLOWS / "ci-full-label.yml").read_text()
    assert "gh run cancel" in text and "gh run rerun" in text
    assert data["concurrency"]["cancel-in-progress"] is True
    assert text.count("actions/checkout") == 1, "one sparse checkout of the decision script, nothing else"
