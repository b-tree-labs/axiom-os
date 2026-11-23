# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""A test that fails in the pre-push run is run once more, alone, before the push is refused.

A test that only fails while the machine is busy blocked pushes of changes that never touched it,
and the way round was ``--no-verify``, which skips every check. The hook now reads the failed tests
from the run's own summary and runs them again serially. A few failures are worth a second look;
many are a real break and are not rerun.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "hooks" / "pre_push_failed.py"
HOOK = ROOT / "scripts" / "hooks" / "pre-push"

SUMMARY = """\
........F...........E...
=========================== short test summary info ============================
FAILED tests/a/test_one.py::test_backlog_is_paced - assert 3 == 2
FAILED tests/a/test_one.py::TestSites::test_two[site a-live] - TimeoutError: waited 5 s
ERROR tests/b/test_two.py::test_needs_database - sqlalchemy.exc.OperationalError
ERROR tests/c/test_three.py - ImportError: no module named nothing
FAILED tests/a/test_one.py::test_backlog_is_paced - assert 3 == 2
3 failed, 2 errors, 900 passed in 61.20s
"""


@pytest.fixture(scope="module")
def helper():
    spec = importlib.util.spec_from_file_location("pre_push_failed_under_test", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_the_failed_tests_are_read_from_the_summary_once_each_in_order(helper):
    assert helper.failed_tests(SUMMARY) == [
        "tests/a/test_one.py::test_backlog_is_paced",
        "tests/a/test_one.py::TestSites::test_two[site a-live]",
        "tests/b/test_two.py::test_needs_database",
        "tests/c/test_three.py",
    ]


def test_a_run_with_no_failures_names_none(helper):
    assert helper.failed_tests("........\n900 passed in 61.20s\n") == []


def test_a_progress_line_that_says_failed_is_not_a_test(helper):
    line = "[gw0] [  0%] FAILED tests/a/test_one.py::test_backlog_is_paced \n"
    assert helper.failed_tests(line) == []


def test_many_failures_are_a_real_break_and_are_not_rerun(helper):
    many = "".join(f"FAILED tests/a/test_one.py::test_{n} - assert False\n" for n in range(11))
    assert helper.rerun_set(many, limit=10) is None
    assert len(helper.rerun_set(many, limit=11)) == 11


def test_the_command_prints_the_tests_and_exits_zero(tmp_path):
    log = tmp_path / "run.log"
    log.write_text(SUMMARY)
    done = subprocess.run([sys.executable, str(SCRIPT), str(log)], capture_output=True, text=True)
    assert done.returncode == 0
    assert done.stdout.splitlines()[0] == "tests/a/test_one.py::test_backlog_is_paced"
    assert len(done.stdout.splitlines()) == 4


@pytest.mark.parametrize(
    "text",
    [
        "900 passed in 61.20s\n",
        "FAILED t.py::a - x\n" * 1 + "".join(f"FAILED t.py::t{n} - x\n" for n in range(20)),
    ],
)
def test_the_command_exits_nonzero_when_there_is_nothing_to_rerun(tmp_path, text):
    log = tmp_path / "run.log"
    log.write_text(text)
    done = subprocess.run([sys.executable, str(SCRIPT), str(log)], capture_output=True, text=True)
    assert done.returncode == 1
    assert done.stdout == ""


def test_the_hook_reruns_failures_alone_and_records_the_ones_that_then_pass():
    hook = HOOK.read_text()
    assert "scripts/hooks/pre_push_failed.py" in hook
    rerun = hook[hook.index("pre_push_failed.py") :]
    assert "-n 0" in rerun
    assert "pre-push-flaky.log" in rerun
    assert "--testmon" not in rerun.split("pre-push-flaky.log")[0]
