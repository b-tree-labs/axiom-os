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
    row = connection.execute("SELECT system_packages, python_version FROM environment").fetchone()
    connection.close()
    assert row == ("newpkg 9.9", "3.13.16")


# --- the pull-request decision ------------------------------------------------
#
# Measured on 2026-10-26: every main run since 02:13 that day was red, the map
# was saved only on green, and every pull request restored the same map from
# 01:34. As main moved, testmon saw 113, then 236 changed files, and the
# selection grew from 88 to 1935 tests. These checks lock the choice the
# workflow makes from a restored map.

import yaml  # noqa: E402

_MAIN_TIP = "76bd82a28e7477f797a4816e454c85eed94ad30e"
_OLDER = "c105c39bc52cee24df9d1efa8db42561545b3d83"
_NARROW = "=========== 731/24984 tests collected (24253 deselected) in 9.80s ===========\n"
_BROAD = "========= 12000/24984 tests collected (12984 deselected) in 9.80s ==========\n"
_WORKFLOW = Path(__file__).parent.parent / ".github" / "workflows" / "ci.yml"


def _decide(
    text=_NARROW, key=f"testmon-sha-Linux-3.13-{_MAIN_TIP}", changed=("src/axiom/a.py",), **kw
):
    return testmon_blast_radius.decide(
        text, key, None if changed is None else list(changed), kw.pop("cap", 1 / 3), **kw
    )


def test_a_map_for_the_base_narrows_and_logs_selected_of_total():
    decision = _decide()
    assert decision.mode == "blast"
    assert (decision.n, decision.total) == (731, 24984)
    assert "731 of 24984" in decision.reason and "76bd82a2" in decision.reason


def test_an_ancestor_map_from_restore_keys_still_narrows():
    """An update-branch merge moves the base sha. The newest older main map is
    restored through restore-keys, and selection runs against its commit."""
    for key in (f"testmon-sha-Linux-3.13-{_OLDER}", f"testmon-Linux-3.13-{_OLDER}"):
        assert testmon_blast_radius.map_sha(key) == _OLDER
        decision = _decide(key=key)
        assert decision.mode == "blast", decision.reason
        assert _OLDER[:8] in decision.reason


def test_a_missing_map_runs_the_full_suite_with_a_reason():
    decision = _decide(text=None, key="")
    assert decision.mode == "full"
    assert decision.reason == "full suite: no blast-radius map was restored"


def test_a_tree_that_cannot_be_diffed_against_the_map_runs_the_full_suite():
    decision = _decide(changed=None)
    assert decision.mode == "full"
    assert "could not diff" in decision.reason


def test_a_changed_conftest_runs_the_full_suite():
    decision = _decide(changed=("src/axiom/a.py", "tests/memory/conftest.py"))
    assert decision.mode == "full"
    assert "conftest" in decision.reason and "tests/memory/conftest.py" in decision.reason


def test_config_dependency_and_fixture_data_changes_run_the_full_suite():
    """Testmon follows executed Python. It does not see these files."""
    for path in (
        "pyproject.toml",
        "packages/axiom-tests/pyproject.toml",
        "uv.lock",
        "requirements-dev.txt",
        "pytest.ini",
        "tests/fixtures/site.json",
        "src/axiom/extensions/builtins/data/tests/testdata/frame.csv",
    ):
        decision = _decide(changed=("src/axiom/a.py", path))
        assert decision.mode == "full", path
        assert path in decision.reason
    # Python under a fixtures directory is executed code, which testmon tracks.
    assert _decide(changed=("tests/fixtures/builders.py",)).mode == "blast"


def test_the_cap_is_a_fraction_of_the_suite_and_says_why():
    decision = _decide(text=_BROAD)
    assert decision.mode == "full"
    assert "12000 of 24984" in decision.reason and "cap 33%" in decision.reason
    assert _decide(text=_BROAD, cap=0.5).mode == "blast"


def test_an_unreadable_selection_runs_the_full_suite():
    assert _decide(text="").mode == "full"
    assert _decide(text="interrupted\n").mode == "full"


def test_the_suite_size_is_the_maps_not_the_collect_denominator(tmp_path):
    """With selection on, testmon skips collecting files it knows are
    unaffected, so ``N/M tests collected`` understates the suite. On #1417's
    first run M was 4289 of 24984, and 1954 selected tripped the cap."""
    import sqlite3

    database = tmp_path / ".testmondata"
    connection = sqlite3.connect(database)
    connection.execute("CREATE TABLE test_execution (id INTEGER PRIMARY KEY, test_name TEXT)")
    connection.executemany(
        "INSERT INTO test_execution (test_name) VALUES (?)", [(f"t{i}",) for i in range(30)]
    )
    connection.commit()
    connection.close()
    # "no tests collected" carries no denominator; an empty selection narrows.
    decision = _decide(text=_ZERO, database=database)
    assert (decision.mode, decision.n, decision.total) == ("blast", 0, 30)
    assert _decide(text=_ZERO).mode == "full"
    connection = sqlite3.connect(database)
    connection.executemany(
        "INSERT INTO test_execution (test_name) VALUES (?)", [(f"u{i}",) for i in range(5970)]
    )
    connection.commit()
    connection.close()
    first_run = "===== 1954/4289 tests collected (2335 deselected) in 19.40s =====\n"
    decision = _decide(text=first_run, database=database)
    assert (decision.mode, decision.n, decision.total) == ("blast", 1954, 6000)


def test_main_decide_prints_the_one_line_reason_and_the_mode(tmp_path, capsys, monkeypatch):
    import io

    changed = tmp_path / "changed.txt"
    changed.write_text("src/axiom/a.py\n")
    monkeypatch.setattr(sys, "stdin", io.StringIO(_NARROW))
    key = f"testmon-sha-Linux-3.13-{_MAIN_TIP}"
    # The default database is .testmondata in the working directory, which in
    # CI is the real map. Point at an absent one so the summary's count is used.
    argv = ["--decide", "--map-key", key, "--changed-files", str(changed)]
    argv += ["--database", str(tmp_path / "absent")]
    assert testmon_blast_radius.main(argv) == 0
    out = capsys.readouterr().out.splitlines()
    assert out[0].startswith("testmon decision: narrowed: 731 of 24984")
    assert "mode=blast" in out and "n=731" in out


def _unit_steps():
    return yaml.safe_load(_WORKFLOW.read_text())["jobs"]["unit-tests"]["steps"]


def test_main_saves_the_map_after_a_red_full_run_too():
    """Testmon reselects failed tests and tests missing from the map, so a red
    run's map is safe. Saving only on green froze the map for a day."""
    save = next(s for s in _unit_steps() if (s.get("uses") or "").startswith("actions/cache/save"))
    condition = save["if"]
    assert "success()" not in condition.replace("== 'success'", "")
    assert "!cancelled()" in condition
    assert "steps.unit-full.outcome == 'failure'" in condition
    assert "github.event_name != 'pull_request'" in condition


def test_pull_requests_decide_against_the_restored_maps_commit():
    steps = _unit_steps()
    restore = next(s for s in steps if (s.get("uses") or "").startswith("actions/cache/restore"))
    assert restore.get("id") == "testmon-map"
    choose = next(s for s in steps if s.get("id") == "unit-mode")["run"]
    assert "steps.testmon-map.outputs.cache-matched-key" in choose
    assert 'git diff --name-only "$map_sha" HEAD' in choose
    assert "--decide" in choose
    assert "-le 8000" not in choose


_VERSION_BUMP = """\
diff --git a/pyproject.toml b/pyproject.toml
--- a/pyproject.toml
+++ b/pyproject.toml
@@ -7 +7 @@ name = "axiom-os-lm"
-version = "0.70.3"
+version = "0.70.7"
"""
_DEPENDENCY_BUMP = (
    _VERSION_BUMP
    + """\
@@ -40 +40 @@ dependencies = [
-    "httpx>=0.27",
+    "httpx>=0.28",
"""
)


def test_a_release_version_bump_alone_keeps_the_selection():
    """Releases bump pyproject's version several times a day. That moves no
    pytest option and no dependency; a dependency line in the same diff does."""
    assert testmon_blast_radius.only_the_version_changed(_VERSION_BUMP)
    assert not testmon_blast_radius.only_the_version_changed(_DEPENDENCY_BUMP)
    assert not testmon_blast_radius.only_the_version_changed("")
    bumped = _decide(changed=("pyproject.toml",), version_only=lambda path: True)
    assert bumped.mode == "blast"
    deps = _decide(changed=("pyproject.toml",), version_only=lambda path: False)
    assert deps.mode == "full"
