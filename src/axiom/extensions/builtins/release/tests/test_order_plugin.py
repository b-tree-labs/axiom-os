# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""The pytest plugin reorders collected tests and records what happened (#2441).

These run a real pytest in a real project, serial and under xdist, because the
two things that matter (every xdist worker agreeing on one order, and the
controller seeing every worker's report) only exist in a real run.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from axiom.extensions.builtins.release.order_history import RunRecord, append_run, read_history

SRC = Path(__file__).resolve().parents[5]  # .../src
PLUGIN = "axiom.extensions.builtins.release.order_plugin"


@pytest.fixture
def project(tmp_path):
    """Three modules. ``test_b.py::test_fails`` fails; the rest pass."""
    (tmp_path / "test_a.py").write_text(
        textwrap.dedent(
            """
            def test_one(): pass
            def test_two(): pass
            """
        )
    )
    (tmp_path / "test_b.py").write_text(
        textwrap.dedent(
            """
            def test_fine(): pass
            def test_fails(): assert False
            """
        )
    )
    (tmp_path / "test_c.py").write_text("def test_three(): pass\n")
    return tmp_path


def _pytest(project, *args, env=None, history=None):
    full = {
        "PATH": os.environ["PATH"],
        "PYTHONPATH": str(SRC),
        "PYTHONDONTWRITEBYTECODE": "1",
        "HOME": str(project),
    }
    if history is not None:
        full["AXIOM_TEST_HISTORY"] = str(history)
    full.update(env or {})
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-p",
            PLUGIN,
            "-p",
            "no:cacheprovider",
            "-o",
            "addopts=",
            "-v",
            *args,
        ],
        cwd=project,
        env=full,
        capture_output=True,
        text=True,
        check=False,
    )


def _executed(output: str) -> list[str]:
    return re.findall(r"^(test_\w+\.py::\w+)\s+(?:PASSED|FAILED)", output, re.MULTILINE)


def _seed(history, failed_ids, passed_ids=()):
    tests = [(i, "f", 0.1) for i in failed_ids] + [(i, "p", 0.1) for i in passed_ids]
    append_run(
        history, RunRecord("0", "a" * 40, "main", "push", "2026-11-25T00:00:00Z", ("src/x",), tests)
    )


def test_without_a_setting_the_order_is_pytests_own_and_nothing_is_written(project):
    result = _pytest(project)
    assert _executed(result.stdout) == [
        "test_a.py::test_one",
        "test_a.py::test_two",
        "test_b.py::test_fine",
        "test_b.py::test_fails",
        "test_c.py::test_three",
    ]
    assert not list(project.glob("*.jsonl*"))


def test_failed_first_runs_the_last_failure_before_everything_else(project):
    history = project / "runs.jsonl"
    _seed(history, ["test_b.py::test_fails"], ["test_a.py::test_one"])
    result = _pytest(project, env={"AXIOM_TEST_ORDER": "failed-first"}, history=history)
    assert _executed(result.stdout)[0] == "test_b.py::test_fails"
    assert len(_executed(result.stdout)) == 5


def test_ranked_order_is_used_only_when_asked_for_by_name(project):
    history = project / "runs.jsonl"
    seen = [
        ("test_a.py::test_one", "p", 9.0),
        ("test_a.py::test_two", "p", 9.0),
        ("test_b.py::test_fine", "p", 9.0),
        ("test_b.py::test_fails", "p", 9.0),
        ("test_c.py::test_three", "p", 0.01),
    ]
    other = project / "other.jsonl"
    for path in (history, other):  # a run records itself, so each run gets its own copy
        append_run(
            path, RunRecord("0", "a" * 40, "main", "push", "2026-11-25T00:00:00Z", ("src/x",), seen)
        )
    default = _pytest(project, history=history)
    ranked = _pytest(project, env={"AXIOM_TEST_ORDER": "ranked"}, history=other)
    assert _executed(default.stdout)[0] == "test_a.py::test_one"
    assert _executed(ranked.stdout)[0] == "test_c.py::test_three", "the cheap file goes first"


def test_an_unknown_order_setting_is_ignored_with_a_warning_not_a_crash(project):
    result = _pytest(project, env={"AXIOM_TEST_ORDER": "fastest"})
    assert "AXIOM_TEST_ORDER" in result.stdout + result.stderr
    assert len(_executed(result.stdout)) == 5


def test_a_run_is_recorded_with_outcomes_durations_and_the_area_changed(project):
    history = project / ".ci-history" / "runs.jsonl.gz"
    changed = project / "changed.txt"
    changed.write_text("src/axiom/memory/store.py\nM\tdocs/guide/x.md\n")
    _pytest(
        project,
        env={
            "AXIOM_TEST_CHANGED_FILES": str(changed),
            "GITHUB_RUN_ID": "777",
            "GITHUB_SHA": "c" * 40,
            "GITHUB_REF_NAME": "feat/x",
            "GITHUB_EVENT_NAME": "pull_request",
        },
        history=history,
    )
    (record,) = read_history(history)
    outcomes = {tid: outcome for tid, outcome, _s in record.tests}
    assert outcomes == {
        "test_a.py::test_one": "p",
        "test_a.py::test_two": "p",
        "test_b.py::test_fine": "p",
        "test_b.py::test_fails": "f",
        "test_c.py::test_three": "p",
    }
    assert record.run == "777" and record.sha == "c" * 40
    assert record.branch == "feat/x" and record.event == "pull_request"
    assert record.prefixes == ("docs/guide", "src/axiom/memory")
    assert record.complete is True


def test_a_pull_request_branch_is_the_head_ref_not_the_merge_ref(project):
    history = project / "runs.jsonl"
    _pytest(
        project,
        env={"GITHUB_REF_NAME": "12/merge", "GITHUB_HEAD_REF": "feat/real"},
        history=history,
    )
    assert read_history(history)[0].branch == "feat/real"


def test_each_run_adds_a_record_and_the_second_run_puts_the_failure_first(project):
    history = project / "runs.jsonl"
    _pytest(project, history=history, env={"AXIOM_TEST_ORDER": "failed-first"})
    second = _pytest(project, history=history, env={"AXIOM_TEST_ORDER": "failed-first"})
    assert len(read_history(history)) == 2
    assert _executed(second.stdout)[0] == "test_b.py::test_fails"


def test_collecting_only_records_nothing(project):
    history = project / "runs.jsonl"
    _pytest(project, "--collect-only", history=history)
    assert not history.exists()


def test_stopping_early_marks_the_run_incomplete(project):
    history = project / "runs.jsonl"
    result = _pytest(project, "--maxfail=1", history=history)
    assert "stopping after 1 failures" in result.stdout
    (record,) = read_history(history)
    assert record.complete is False
    assert {tid for tid, _o, _s in record.tests} < {
        "test_a.py::test_one",
        "test_a.py::test_two",
        "test_b.py::test_fine",
        "test_b.py::test_fails",
        "test_c.py::test_three",
    } | {"test_b.py::test_fails"}


def test_an_unwritable_history_never_changes_the_verdict(project):
    blocked = project / "blocked"
    blocked.write_text("a file where the directory should be")
    result = _pytest(project, history=blocked / "runs.jsonl")
    assert "1 failed, 4 passed" in result.stdout


def test_a_corrupt_history_never_changes_the_verdict(project):
    history = project / "runs.jsonl"
    history.write_text("\x00\x01 not a history\n")
    result = _pytest(project, history=history, env={"AXIOM_TEST_ORDER": "ranked"})
    assert "1 failed, 4 passed" in result.stdout


def test_under_xdist_every_worker_agrees_on_the_order_and_the_controller_records_once(project):
    history = project / "runs.jsonl"
    _seed(history, ["test_b.py::test_fails"], ["test_a.py::test_one", "test_c.py::test_three"])
    for dist in ("load", "worksteal"):
        result = _pytest(
            project,
            "-n",
            "2",
            f"--dist={dist}",
            env={"AXIOM_TEST_ORDER": "ranked"},
            history=history,
        )
        assert "Different tests were collected" not in result.stdout + result.stderr, dist
        assert "1 failed, 4 passed" in result.stdout, (dist, result.stdout[-400:])
    runs = read_history(history)
    assert len(runs) == 3, "one record per run, however many workers"
    assert len(runs[-1].tests) == 5
    assert runs[-1].complete is True


def test_under_xdist_maxfail_still_marks_the_run_incomplete(project):
    (project / "test_d.py").write_text(
        "\n".join(f"def test_ok{i}(): pass" for i in range(40)) + "\n"
    )
    history = project / "runs.jsonl"
    _pytest(project, "-n", "2", "--maxfail=1", history=history)
    (record,) = read_history(history)
    assert record.complete is False
