# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""CI finds a failure early (#2441).

On 2026-11-25 a retired-word guard that takes seconds failed 14 minutes into a
20-minute unit run. These checks lock the shape that prevents it: the repo-wide
guards run first and gate the long job, a pull request stops at its first real
failures, the tail is balanced, and the order history is carried between runs
by the same cache pattern that carries the blast-radius map.
"""

from __future__ import annotations

import re
import shlex
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
CI = yaml.safe_load((ROOT / ".github" / "workflows" / "ci.yml").read_text())


def _steps(job: str) -> list[dict]:
    return [s for s in CI["jobs"][job]["steps"] if isinstance(s, dict)]


def _index(job: str, name: str) -> int:
    names = [s.get("name") for s in _steps(job)]
    assert name in names, f"{job} has no step named {name!r}; it has {names}"
    return names.index(name)


def _step(job: str, name: str) -> dict:
    return _steps(job)[_index(job, name)]


# --- 1. guards first ----------------------------------------------------------


def test_the_repo_wide_guards_run_before_anything_else_that_takes_time():
    """Right after the install, before compile and before collecting the suite."""
    guard = _index("smoke", "Repo-wide guards")
    assert guard < _index("smoke", "Import and compile")
    assert guard < _index("smoke", "Collect every test")
    assert guard == _index("smoke", "Install dependencies") + 1


def test_the_guard_step_runs_the_one_declared_list_in_process():
    run = _step("smoke", "Repo-wide guards")["run"]
    assert "-n 0" in run
    assert "axiom.extensions.builtins.release.ci_guards" in run
    assert "--ci-targets" in run
    assert _step("smoke", "Repo-wide guards")["if"] == "needs.changes.outputs.code == 'true'"


def test_a_red_guard_means_the_long_job_never_starts_work():
    """The unit job needs Smoke and fails its own leg inside seconds when Smoke is red."""
    unit = CI["jobs"]["unit-tests"]
    assert "smoke" in unit["needs"]
    stop = _step("unit-tests", "Stop when Smoke or Lint failed")
    assert "needs.smoke.result != 'success'" in stop["if"]
    assert _index("unit-tests", "Stop when Smoke or Lint failed") < _index(
        "unit-tests", "Install dependencies"
    )


def test_run_unit_tests_stays_a_bare_pytest_with_no_path_arguments():
    for name in (
        "Run unit tests",
        "Run every unit test and refresh the blast-radius map",
    ):
        argv = shlex.split(_step("unit-tests", name)["run"])
        positional = []
        skip = False
        for arg in argv[1:]:
            if skip:
                skip = False
            elif arg in {"-n", "-m", "-k", "-p", "--tb", "--junitxml", "--timeout"}:
                skip = True
            elif not arg.startswith("-"):
                positional.append(arg)
        assert not positional, (name, positional)


# --- 7 and 8. early stop on pull requests, balanced tail ------------------------


def _unit_runs() -> list[str]:
    return [
        _step("unit-tests", "Run unit tests")["run"],
        _step("unit-tests", "Run every unit test and refresh the blast-radius map")["run"],
    ]


def test_both_unit_runs_balance_the_tail_with_worksteal():
    for run in _unit_runs():
        assert "--dist=worksteal" in run


def test_both_unit_runs_stop_early_only_through_the_one_maxfail_variable():
    for run in _unit_runs():
        assert '--maxfail="$maxfail"' in run


def test_only_a_pull_request_sets_a_failure_limit():
    """main, the merge queue and a pull request labelled ci:full (a full run) run every test and
    refresh the map: maxfail 0 is no limit."""
    run = _step("unit-tests", "Choose the unit-test run")["run"]
    head, _, rest = run.partition("maxfail=0\n")
    assert rest, "maxfail starts at 0, which pytest reads as no limit"
    assert re.match(
        r'\s*if \[ "\$\{\{ github.event_name \}\}" = "pull_request" \] && \[ "\$FULL_RUN" != "true" \]; then\n'
        r'\s*maxfail="\$\{\{ vars.AXIOM_PR_MAXFAIL \|\| 5 \}\}"\n\s*fi\n',
        rest,
    ), "only the pull_request branch raises it"
    assert 'echo "maxfail=${maxfail}" >> "$GITHUB_ENV"' in rest


# --- 3, 4, 5. failed-first, ranking, a history that outlives artifacts ------------


def test_the_order_history_rides_the_cache_pattern_the_blast_radius_map_uses():
    restore = _step("unit-tests", "Test history from earlier runs")
    assert restore["uses"].startswith("actions/cache/restore")
    assert restore["with"]["path"] == "${{ runner.temp }}/ci-history"
    assert restore["with"]["key"].startswith("test-history-${{ runner.os }}-")
    assert "github.run_id" in restore["with"]["key"], (
        "a fixed key is immutable and would never refresh"
    )
    assert restore["with"]["restore-keys"].strip() == "test-history-${{ runner.os }}-"
    save = _step("unit-tests", "Save the test history")
    assert save["uses"].startswith("actions/cache/save")
    assert save["with"]["path"] == "${{ runner.temp }}/ci-history"
    assert save["with"]["key"] == restore["with"]["key"]


def test_the_history_is_saved_after_a_red_run_and_never_after_a_cancelled_one():
    condition = _step("unit-tests", "Save the test history")["if"]
    assert "!cancelled()" in condition
    assert "needs.changes.outputs.code == 'true'" in condition
    for outcome in (
        "steps.unit-full.outcome == 'failure'",
        "steps.unit-blast.outcome == 'failure'",
        "steps.unit-full.outcome == 'success'",
        "steps.unit-blast.outcome == 'success'",
    ):
        assert outcome in condition, "a history is saved only when a test run happened"


def test_the_history_is_restored_before_the_tests_that_read_it_and_saved_after():
    assert _index("unit-tests", "Test history from earlier runs") < _index(
        "unit-tests", "Run unit tests"
    )
    assert _index("unit-tests", "Save the test history") > _index(
        "unit-tests",
        "Run every unit test and refresh the blast-radius map",
    )


_RUNS = ("Run unit tests", "Run every unit test and refresh the blast-radius map")


def test_the_unit_runs_load_the_order_plugin_through_the_environment_not_their_arguments():
    """A bare pytest names no plugin: the plugin arrives with PYTEST_ADDOPTS. The
    axiom-tests step does not get it, so its own rootdir is left alone."""
    for name in _RUNS:
        env = _step("unit-tests", name)["env"]
        assert "-p axiom.extensions.builtins.release.order_plugin" in env["PYTEST_ADDOPTS"]
        assert env["AXIOM_TEST_HISTORY"] == "${{ runner.temp }}/ci-history/runs.jsonl.gz"
    assert "env" not in _step("unit-tests", "Run axiom-tests package tests")
    assert "PYTEST_ADDOPTS" not in CI["jobs"]["unit-tests"].get("env", {})


def test_the_ranker_is_off_in_ci_until_the_replay_shows_a_gain():
    """Failed-first is on. Ranked order needs an explicit repository variable."""
    for name in _RUNS:
        setting = _step("unit-tests", name)["env"]["AXIOM_TEST_ORDER"]
        assert setting == "${{ vars.AXIOM_TEST_ORDER || 'failed-first' }}"


def test_the_changed_paths_the_history_records_are_written_for_every_event():
    step = _step("unit-tests", "Changed paths for the test history")
    assert "AXIOM_TEST_CHANGED_FILES" in step["run"]
    assert _index("unit-tests", "Changed paths for the test history") < _index(
        "unit-tests", "Run unit tests"
    )


# --- 6. shadow mode -----------------------------------------------------------


def test_the_shadow_replay_reports_on_main_and_never_fails_a_run():
    step = _step("unit-tests", "Shadow replay of the test order")
    assert step.get("continue-on-error") is True
    assert "github.event_name != 'pull_request'" in step["if"]
    assert "axiom.extensions.builtins.release.order_replay" in step["run"]
    assert "GITHUB_STEP_SUMMARY" in step["run"]


# --- 11 and 9. full runs start the long work at once and split it into shards -----------------


SHARDS = 3


def _shard_job() -> dict:
    return CI["jobs"]["unit-shards"]


def test_the_long_work_on_main_does_not_wait_for_smoke_or_lint():
    """Guards-first matters on a pull request, where the guards decide whether to pay for the
    suite at all. On main the suite is going to run, so it starts with Smoke and Lint."""
    assert set(_shard_job()["needs"]) <= {"changes", "tag-already-green"}
    assert "smoke" not in _shard_job()["needs"] and "lint" not in _shard_job()["needs"]


def test_the_shards_are_for_full_runs_only_and_have_a_switch():
    condition = _shard_job()["if"]
    assert "needs.changes.outputs.full == 'true'" in condition
    assert "vars.AXIOM_UNIT_SHARD != 'off'" in condition
    assert "needs.tag-already-green.outputs.skip != 'true'" in condition
    assert "needs.changes.outputs.code == 'true'" in condition


def test_the_shard_count_is_one_number_everywhere():
    matrix = _shard_job()["strategy"]["matrix"]["shard"]
    assert matrix == list(range(1, SHARDS + 1))
    assert _shard_job()["strategy"]["fail-fast"] is False, "a red run still writes a complete map"
    run = _step("unit-shards", "Run unit tests")
    assert run["env"]["AXIOM_TEST_SHARD"] == f"${{{{ matrix.shard }}}}/{SHARDS}"
    verify = _step("unit-tests", "Every collected test ran in exactly one shard")["run"]
    assert f"--shards {SHARDS}" in verify


def test_a_shard_runs_the_bare_pytest_the_collection_contract_reads():
    step = _step("unit-shards", "Run unit tests")
    argv = shlex.split(step["run"])
    positional = []
    skip = False
    for arg in argv[1:]:
        if skip:
            skip = False
        elif arg in {"-n", "-m", "-k", "-p", "--tb", "--junitxml", "--timeout"}:
            skip = True
        elif not arg.startswith("-"):
            positional.append(arg)
    assert not positional, positional
    assert "--dist=worksteal" in step["run"] and "--testmon-noselect" in step["run"]
    assert "--maxfail" not in step["run"], "a full run runs every test"
    env = step["env"]
    assert "-p axiom.extensions.builtins.release.order_plugin" in env["PYTEST_ADDOPTS"]
    assert env["AXIOM_TEST_SHARD_REPORT"] and env["AXIOM_TEST_HISTORY_OUT"]


def test_a_shard_has_what_the_suite_needs_a_database_and_the_same_venv_cache():
    assert "postgres" in _shard_job()["services"]
    cache = next(s for s in _steps("unit-shards") if s.get("name") == "Cache venv")
    unit_cache = _step("unit-tests", "Cache venv")
    assert cache["with"]["key"] == unit_cache["with"]["key"]
    assert "vars.CI_RUNNER == 'gcp'" in _shard_job()["runs-on"]


def test_every_shard_hands_its_results_to_the_one_job_that_joins_them():
    upload = next(
        s
        for s in _steps("unit-shards")
        if "actions/upload-artifact" in s.get("uses", "")
        and "unit-shard-" in str(s["with"]["name"])
    )
    assert upload["with"]["name"] == "unit-shard-${{ matrix.shard }}"
    assert "!cancelled()" in upload["if"]
    assert int(upload["with"]["retention-days"]) <= 3


def test_the_unit_check_stays_one_required_context_and_waits_for_the_shards():
    unit = CI["jobs"]["unit-tests"]
    assert {"smoke", "lint", "unit-shards"} <= set(unit["needs"])
    assert unit["strategy"]["matrix"]["python-version"] == ["3.13"], (
        "the required context is Unit Tests (3.13)"
    )
    assert unit["name"] == "Unit Tests"


def test_a_full_run_joins_on_a_hosted_runner_and_a_pull_request_keeps_its_own_path():
    runs_on = CI["jobs"]["unit-tests"]["runs-on"]
    assert (
        "AXIOM_UNIT_SHARD != 'off'" in runs_on and "needs.changes.outputs.full == 'true'" in runs_on
    )
    assert runs_on.rstrip(" }").endswith("'ubuntu-24.04'")
    env = CI["jobs"]["unit-tests"]["env"]
    assert (
        env["UNIT_SHARDED"]
        == "${{ needs.changes.outputs.full == 'true' && vars.AXIOM_UNIT_SHARD != 'off' }}"
    )


_PR_ONLY = (
    "Choose the unit-test run",
    "Run unit tests",
    "Run every unit test and refresh the blast-radius map",
    "Save the blast-radius map",
    "Run axiom-tests package tests",
    "Upload test results",
)


def test_the_pull_request_steps_do_not_run_when_the_shards_did_the_work():
    for name in _PR_ONLY:
        assert "env.UNIT_SHARDED != 'true'" in _step("unit-tests", name)["if"], name


def test_the_pull_request_path_is_the_one_it_was():
    """Blast selection from the cached map is untouched: the same decision script, flags and fallbacks."""
    choose = _step("unit-tests", "Choose the unit-test run")["run"]
    for kept in (
        "scripts/testmon_blast_radius.py --align .testmondata",
        "--testmon --testmon-forceselect --collect-only",
        "--decide",
        "--decide --no-selection",
        'git diff --name-status --no-renames "$map_sha" HEAD',
    ):
        assert kept in choose, kept
    assert (
        '.venv/bin/pytest -n "$blast_workers" --testmon --testmon-forceselect'
        in _step("unit-tests", "Run unit tests")["run"]
    )
    assert (
        '.venv/bin/pytest -n "$workers" --testmon-noselect'
        in _step("unit-tests", "Run every unit test and refresh the blast-radius map")["run"]
    )


def test_a_failed_shard_fails_the_required_check_after_the_maps_and_history_are_joined():
    names = [s.get("name") for s in _steps("unit-tests")]
    fail = _step("unit-tests", "Fail when a shard failed")
    assert "needs.unit-shards.result != 'success'" in fail["if"]
    assert "!cancelled()" in fail["if"]
    assert names.index("Fail when a shard failed") > names.index(
        "Every collected test ran in exactly one shard"
    )
    assert names.index("Fail when a shard failed") > names.index("Join the shard blast-radius maps")


def test_the_union_of_shards_is_checked_before_anything_is_saved():
    names = [s.get("name") for s in _steps("unit-tests")]
    verify = names.index("Every collected test ran in exactly one shard")
    assert verify < names.index("Join the shard blast-radius maps")
    assert verify < names.index("Save the joined blast-radius map")
    assert (
        "order_shard verify"
        in _step("unit-tests", "Every collected test ran in exactly one shard")["run"]
    )


def test_the_joined_map_is_saved_under_the_key_a_pull_request_restores_and_only_when_the_join_worked():
    join = _step("unit-tests", "Join the shard blast-radius maps")
    assert join["id"] == "join-map" and join.get("continue-on-error") is True
    assert "testmon_merge" in join["run"]
    save = _step("unit-tests", "Save the joined blast-radius map")
    old = _step("unit-tests", "Save the blast-radius map")
    assert save["with"]["key"] == old["with"]["key"] and save["with"]["path"] == ".testmondata"
    assert "steps.join-map.outcome == 'success'" in save["if"]
    assert "steps.verify-shards.outcome == 'success'" in save["if"]


def test_a_map_that_cannot_be_joined_is_never_replaced_by_a_partial_one():
    run = _step("unit-tests", "Join the shard blast-radius maps")["run"]
    assert "--out" in run and "|| exit" not in run


def test_the_shard_job_wires_item_one_the_unit_contract_still_collects_the_testpaths():
    """The collection contract reads every step named Run unit tests, shards included."""
    names = [s.get("name") for s in _steps("unit-shards")]
    assert names.count("Run unit tests") == 1
