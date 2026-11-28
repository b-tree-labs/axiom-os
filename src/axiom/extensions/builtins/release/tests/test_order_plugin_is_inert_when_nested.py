# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""A pytest a test launches is not the CI run, and the run's settings are not its own (#2441).

The first sharded main run (45723309744) failed in all three shards. The shard step sets
``PYTEST_ADDOPTS`` and ``AXIOM_TEST_*`` in the environment, and many tests launch a pytest of
their own (the extension standard tests, ``axi ext test``, the isolation guard, the acceptance
harness), in a subprocess or in process. Those inherited the settings: the shard selection
deselected their tests ("11 deselected", exit 5), and they wrote the shard's report, so the
shards' totals and digests no longer agreed ("collected different suites").

The plugin now acts only in the outermost session (and its xdist workers). A pytest started
under one, by any route, does nothing: no shard, no reorder, no recording.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from axiom.extensions.builtins.release.order_shard import (
    ShardReport,
    assign,
    load_table,
    shard_of,
    verify,
)

SRC = Path(__file__).resolve().parents[5]
REPO = SRC.parent
PLUGIN = "axiom.extensions.builtins.release.order_plugin"


def _env(project: Path, **extra: str) -> dict[str, str]:
    env = {
        "PATH": os.environ["PATH"],
        "PYTHONPATH": str(SRC),
        "HOME": str(project),
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTEST_ADDOPTS": f"-p {PLUGIN}",
    }
    env.update(extra)
    return env


def _run(project: Path, *args: str, env: dict[str, str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-o", "addopts=", *args],
        cwd=project,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.fixture
def project(tmp_path):
    """One module that launches pytests of its own, and a small inner project to launch them on."""
    inner = tmp_path / "inner"
    inner.mkdir()
    # modules on both sides of the 2-way split, so a shard that leaked in would drop some of them
    names = [f"test_inner{k}.py" for k in range(12)]
    assert {shard_of(n, {}, 2) for n in names} == {0, 1}
    for name in names:
        (inner / name).write_text("def test_a(): pass\ndef test_b(): pass\n")
    outer = tmp_path / "outer"
    outer.mkdir()
    (outer / "test_launcher.py").write_text(
        textwrap.dedent(f"""
        import subprocess, sys
        import pytest

        def test_a_subprocess_pytest_runs_everything_it_was_given():
            done = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
                                   "-o", "addopts=", r"{inner}"], capture_output=True, text=True)
            assert done.returncode == 0, done.stdout + done.stderr
            assert "24 passed" in done.stdout and "deselected" not in done.stdout

        def test_an_in_process_pytest_runs_everything_it_was_given():
            code = pytest.main(["-q", "-p", "no:cacheprovider", "-o", "addopts=", r"{inner}"])
            assert code == 0
        """)
    )
    (outer / "test_other.py").write_text("def test_x(): pass\n")
    return tmp_path


def _shard_holding(module: str, count: int = 2) -> str:
    """The ``i/count`` spec whose shard keeps ``module`` (the table is empty in the scratch project)."""
    return f"{shard_of(module, {}, count) + 1}/{count}"


def test_a_nested_pytest_sees_no_shard_effect_in_a_subprocess_or_in_process(project, tmp_path):
    outer = project / "outer"
    spec = _shard_holding("test_launcher.py")
    done = _run(
        outer,
        "-n",
        "1",
        env=_env(
            outer, AXIOM_TEST_SHARD=spec, AXIOM_TEST_SHARD_REPORT=str(tmp_path / "report.json")
        ),
    )
    assert done.returncode == 0 and "failed" not in done.stdout, done.stdout + done.stderr


def test_a_nested_pytest_does_not_write_the_shards_report_or_history(project, tmp_path):
    outer = project / "outer"
    report = tmp_path / "report.json"
    history = tmp_path / "history.jsonl"
    _run(
        outer,
        "-n",
        "1",
        env=_env(
            outer,
            AXIOM_TEST_SHARD=_shard_holding("test_launcher.py"),
            AXIOM_TEST_SHARD_REPORT=str(report),
            AXIOM_TEST_HISTORY=str(history),
        ),
    )
    body = json.loads(report.read_text())
    assert body["total"] == 3, "the outer suite's three tests, not an inner pytest's twenty-four"
    from axiom.extensions.builtins.release.order_history import read_history

    (record,) = read_history(history)
    assert {t.partition("::")[0] for t, _o, _s in record.tests} <= {
        "test_launcher.py",
        "test_other.py",
    }


def test_every_shard_reports_the_same_suite_even_when_tests_launch_pytests(project, tmp_path):
    outer = project / "outer"
    reports = []
    for index in (1, 2):
        path = tmp_path / f"r{index}.json"
        _run(
            outer,
            "-n",
            "1",
            env=_env(outer, AXIOM_TEST_SHARD=f"{index}/2", AXIOM_TEST_SHARD_REPORT=str(path)),
        )
        reports.append(ShardReport(**json.loads(path.read_text())))
    assert reports[0].total == reports[1].total == 3
    assert reports[0].total_digest == reports[1].total_digest
    assert verify(reports, expected_shards=2) == []


def test_the_isolation_guard_and_the_environment_guards_pass_with_the_shard_settings_active(
    tmp_path,
):
    """The real tests that failed on main: they launch pytests of their own."""
    table = load_table(REPO / "scripts" / "unit_shard_durations.json")
    for module in ("tests/test_test_isolation_guard.py", "tests/test_git_env_neutralised.py"):
        spec = f"{shard_of(module, assign(table, 3), 3) + 1}/3"
        env = _env(
            REPO,
            AXIOM_TEST_SHARD=spec,
            AXIOM_TEST_SHARD_REPORT=str(tmp_path / "r.json"),
            AXIOM_TEST_HISTORY=str(tmp_path / "h.jsonl.gz"),
            AXIOM_TEST_ORDER="failed-first",
        )
        env["PYTHONPATH"] = str(SRC)
        done = subprocess.run(
            [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-n", "2", module],
            cwd=REPO,
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        assert done.returncode == 0, f"{module} under shard {spec}:\n{done.stdout[-1500:]}"


def test_a_pytest_started_inside_a_worker_is_nested_too(project, tmp_path):
    """The workers belong to the run; a pytest a worker's test launches does not."""
    outer = project / "outer"
    done = _run(
        outer, "-n", "2", env=_env(outer, AXIOM_TEST_SHARD=_shard_holding("test_launcher.py"))
    )
    assert done.returncode == 0 and "failed" not in done.stdout, done.stdout + done.stderr
