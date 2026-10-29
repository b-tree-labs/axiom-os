# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""CI spends its minutes on purpose.

These checks lock the cost controls in the workflows: one interpreter on the
pull-request and main path, the others on a nightly that fails loudly, a venv
cache that is allowed to survive, and a chart workflow that cannot run forever
or pile superseded pull requests on top of each other.
"""

from __future__ import annotations

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github" / "workflows"

VENV_KEY = "hashFiles('**/pyproject.toml', '**/uv.lock', '**/poetry.lock', '**/Pipfile.lock')"


def _load(name: str) -> dict:
    return yaml.safe_load((WORKFLOWS / name).read_text())


def _runs(job: dict) -> str:
    return "\n".join(step.get("run", "") for step in job.get("steps", []) if isinstance(step, dict))


def test_pull_requests_and_main_run_only_python_313():
    workflow = _load("ci.yml")
    matrix = workflow["jobs"]["unit-tests"]["strategy"]["matrix"]["python-version"]
    assert matrix == ["3.13"]
    text = (WORKFLOWS / "ci.yml").read_text()
    assert '["3.11","3.12","3.13"]' not in text
    assert "preflight-deps" not in workflow["jobs"]


def test_a_newer_main_push_supersedes_an_older_one_but_tags_and_queue_runs_finish():
    """Until a merge queue exists, main pushes are cancelled by the next push to
    main. Tag and merge-queue runs keep their own group and are not cancelled."""
    concurrency = _load("ci.yml")["concurrency"]
    assert "'main'" in concurrency["group"]
    assert "github.ref" in concurrency["group"]
    assert "refs/heads/main" in concurrency["cancel-in-progress"]
    assert "pull_request" in concurrency["cancel-in-progress"]
    assert "refs/tags" not in concurrency["cancel-in-progress"]
    assert "merge_group" not in concurrency["cancel-in-progress"]


def test_the_unit_job_waits_for_lint_and_smoke():
    workflow = _load("ci.yml")
    unit = workflow["jobs"]["unit-tests"]
    assert {"smoke", "lint"} <= set(unit["needs"])
    # A job-level skip would report success to branch protection and Smoke is not
    # a required check, so the gate is a failing first step, not an `if:`.
    assert "needs.smoke.result" not in unit["if"]
    gate = next(step for step in unit["steps"] if step.get("name") == "Stop when Smoke or Lint failed")
    assert "needs.smoke.result != 'success'" in gate["if"]
    assert "needs.lint.result != 'success'" in gate["if"]


def test_a_push_to_main_skips_the_suite_when_the_merge_queue_already_tested_the_tree():
    gate = _load("ci.yml")["jobs"]["tag-already-green"]
    assert "refs/heads/main" in gate["if"]
    check = _runs(gate)
    assert "event=merge_group" in check
    assert "tree_id" in check


def test_older_interpreters_run_weekly_and_on_demand_with_a_failure_signal():
    # PyYAML 1.1 reads the workflow key `on` as boolean true, so the trigger
    # block is checked from the file text.
    text = (WORKFLOWS / "ci-python-compat.yml").read_text()
    assert "schedule:" in text
    assert "workflow_dispatch:" in text
    assert 'cron: "17 7 * * 1"' in text
    workflow = _load("ci-python-compat.yml")
    assert workflow["jobs"]["unit-tests"]["strategy"]["fail-fast"] is False
    signal = workflow["jobs"]["signal"]
    assert "needs.unit-tests.result != 'success'" in signal["if"]
    text = (WORKFLOWS / "ci-python-compat.yml").read_text()
    assert "::error" in text
    assert "issues.create" in text
    assert "3.11,3.12" in text


def test_a_tag_of_a_commit_already_green_on_main_skips_the_suite_and_still_builds():
    workflow = _load("ci.yml")
    gate = workflow["jobs"]["tag-already-green"]
    assert "refs/tags/v" in gate["if"]
    check = _runs(gate)
    assert "actions/workflows/ci.yml/runs" in check
    assert "git rev-parse HEAD" in check
    build_if = workflow["jobs"]["build"]["if"]
    assert "tag-already-green.outputs.skip == 'true'" in build_if
    unit_if = workflow["jobs"]["unit-tests"]["if"]
    assert "tag-already-green.outputs.skip != 'true'" in unit_if


def test_venv_cache_survives_a_hit_and_is_keyed_on_every_project_file():
    for path in sorted(WORKFLOWS.glob("*.yml")):
        workflow = yaml.safe_load(path.read_text())
        for job_name, job in workflow.get("jobs", {}).items():
            for step in job.get("steps", []) or []:
                if not isinstance(step, dict):
                    continue
                with_ = step.get("with") or {}
                if step.get("uses", "").startswith("actions/cache") and with_.get("path") == ".venv":
                    key = with_["key"]
                    assert VENV_KEY in key, f"{path.name} {job_name} cache key is {key}"
                    assert "restore-keys" not in with_, f"{path.name} {job_name} restores a stale venv"
                run = step.get("run") or ""
                if "python -m venv --clear" in run:
                    assert "if ! .venv/bin/python" in run, (
                        f"{path.name} {job_name} clears a restored venv unconditionally"
                    )


def test_the_blast_radius_map_prefers_this_commit_then_the_newest_older_map():
    """Selection compares file contents, so an older map is a real diff.

    The exact key is still the base sha. A miss falls back to the newest
    ``testmon-sha-<os>-3.13-`` map, then to a map saved under the previous
    ``testmon-<os>-3.13-`` key, so a pull request does not wait out a main
    run and does not throw away maps already in the cache. A pull request
    still must not save: the key is immutable, and the first one to write
    would store its own tree under the base sha.
    """
    steps = [
        step
        for step in _load("ci.yml")["jobs"]["unit-tests"]["steps"]
        if isinstance(step, dict)
        and (step.get("with") or {}).get("path") == ".testmondata"
    ]
    assert steps, "the unit job no longer keeps a blast-radius map"
    for step in steps:
        with_ = step["with"]
        assert "testmon-sha-" in with_["key"]
        uses = step.get("uses") or ""
        if uses.startswith("actions/cache/save"):
            assert "restore-keys" not in with_, step.get("name")
            assert "pull_request" in (step.get("if") or "")
            assert "github.sha" in with_["key"]
        else:
            assert "base.sha" in with_["key"]
            fallback = with_.get("restore-keys") or ""
            assert "testmon-sha-${{ runner.os }}-3.13-" in fallback
            assert "testmon-${{ runner.os }}-3.13-" in fallback


def test_pull_requests_force_testmon_selection_and_parallelize_a_large_set():
    """``addopts`` includes ``-m``. Testmon deactivates selection when a mark
    expression is set, so ``pytest --testmon`` collects the whole suite and
    the cap sends every pull request through the full run. ``--testmon-forceselect``
    keeps the marker and the selection. Zero collected tests is a blast
    radius of zero. A large selection uses the same worker count as the full
    suite; a small one passes ``-n 0``, which runs in-process.
    """
    run = _runs(_load("ci.yml")["jobs"]["unit-tests"])
    assert "--testmon --testmon-forceselect --collect-only" in run
    assert "scripts/testmon_blast_radius.py --align .testmondata" in run
    assert "scripts/testmon_blast_radius.py" in run
    assert '-ge 32' in run
    assert '.venv/bin/pytest -n "$blast_workers" --testmon --testmon-forceselect' in run
    assert '.venv/bin/pytest -n "$workers" --testmon-noselect' in run


def test_unit_workers_track_cpus_instead_of_the_physical_default():
    run = _runs(_load("ci.yml")["jobs"]["unit-tests"])
    assert "sched_getaffinity" in run
    assert '.venv/bin/pytest -n "$workers"' in run


def test_node_chart_cancels_superseded_pull_requests_and_times_out():
    workflow = _load("node-chart.yml")
    assert workflow["concurrency"]["cancel-in-progress"] == "${{ github.event_name == 'pull_request' }}"
    assert "github.ref" in workflow["concurrency"]["group"]
    for name, job in workflow["jobs"].items():
        assert isinstance(job.get("timeout-minutes"), int), name
        assert job["timeout-minutes"] >= 15, name
    assert workflow["jobs"]["chain"]["timeout-minutes"] >= 60
    assert workflow["jobs"]["zero-gap"]["timeout-minutes"] >= 60


def test_adr_numbers_are_checked_on_docs_only_pull_requests():
    """A docs-only pull request short-circuits Unit Tests, so an ADR-only
    change never ran the numbering tests and could claim a number main
    already holds (main went red on a second ADR-167 on 2026-10-10). Lint is
    required and has no docs-only gate, so the check lives there."""
    workflow = _load("ci.yml")
    lint = workflow["jobs"]["lint"]
    assert "if" not in lint
    assert "changes" not in (lint.get("needs") or [])
    adr_steps = [
        step
        for step in lint["steps"]
        if isinstance(step, dict) and "lint_adr_numbers.py" in (step.get("run") or "")
    ]
    assert len(adr_steps) == 1
    step = adr_steps[0]
    assert "if" not in step
    run = step["run"]
    assert "python scripts/lint_adr_numbers.py\n" in run
    assert "tests/test_adr_numbering.py" in run
    assert "tests/test_lint_adr_numbers.py" in run
    assert lint["name"] == "Lint"
