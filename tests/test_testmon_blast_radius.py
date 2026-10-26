# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""The pull-request gate reads pytest's collect summary, not a guess.

``addopts`` carries ``-m``. pytest-testmon turns selection off when a mark
expression is set, so ``pytest --testmon --collect-only`` prints the whole
suite and the gate treats that as the blast radius. ``--testmon-forceselect``
keeps the marker and the selection. These checks lock the summary parser the
workflow uses for that output.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

_SCRIPT = Path(__file__).parent.parent / "scripts" / "testmon_blast_radius.py"
_spec = importlib.util.spec_from_file_location("testmon_blast_radius", _SCRIPT)
assert _spec and _spec.loader
testmon_blast_radius = importlib.util.module_from_spec(_spec)
sys.modules["testmon_blast_radius"] = testmon_blast_radius
_spec.loader.exec_module(testmon_blast_radius)


_ZERO = """\
============================= test session starts ==============================
testmon: changed files: tests/test_b.py, unchanged files: 3, environment: default
collected 2 items / 2 deselected / 0 selected

================== no tests collected (2 deselected) in 0.00s ==================
"""

_SOME = """\
testmon: changed files: tests/test_a.py, unchanged files: 3, environment: default
collected 4 items / 2 deselected / 2 selected

================= 2/4 tests collected (2 deselected) in 0.00s ==================
"""

_QUIET_FULL = "24902 tests collected in 73.12s\n"
_QUIET_RATIO = "12/24902 tests collected (24890 deselected) in 4.20s\n"


def test_an_unchanged_tree_is_zero_tests_not_an_unreadable_summary():
    assert testmon_blast_radius.selected_count(_ZERO) == 0


def test_the_selected_count_is_the_numerator_not_the_deselected_tail():
    assert testmon_blast_radius.selected_count(_SOME) == 2
    assert testmon_blast_radius.selected_count(_QUIET_RATIO) == 12


def test_a_summary_with_no_ratio_is_the_leading_count():
    assert testmon_blast_radius.selected_count(_QUIET_FULL) == 24902


def test_a_collect_error_is_unreadable_so_the_suite_stays_whole():
    assert testmon_blast_radius.selected_count("") is None
    assert testmon_blast_radius.selected_count("2 errors in 0.32s\n") is None
    assert testmon_blast_radius.selected_count("2 deselected in 0.00s\n") is None


def test_main_prints_n_or_exits_unreadable(capsys):
    assert testmon_blast_radius.main_text(_SOME) == 0
    assert capsys.readouterr().out.strip() == "n=2"
    assert testmon_blast_radius.main_text("interrupted\n") == 2


def test_align_environment_keeps_the_rows_and_rewrites_the_fingerprint(tmp_path):
    """A minor bump of any distribution makes testmon delete the environment
    and select every test. The blast radius is the source diff, so the stored
    fingerprint is moved to this interpreter before selection."""
    import sqlite3

    database = tmp_path / ".testmondata"
    connection = sqlite3.connect(database)
    connection.execute(
        "CREATE TABLE environment (id INTEGER PRIMARY KEY, environment_name TEXT,"
        " system_packages TEXT, python_version TEXT)"
    )
    connection.execute(
        "INSERT INTO environment (environment_name, system_packages, python_version)"
        " VALUES ('default', 'oldpkg 1.2', '3.13.1')"
    )
    connection.commit()
    connection.close()

    updated = testmon_blast_radius.align_environment(
        database, packages="newpkg 9.9", python_version="3.13.16"
    )
    assert updated == 1
    connection = sqlite3.connect(database)
    row = connection.execute(
        "SELECT system_packages, python_version FROM environment"
    ).fetchone()
    connection.close()
    assert row == ("newpkg 9.9", "3.13.16")
