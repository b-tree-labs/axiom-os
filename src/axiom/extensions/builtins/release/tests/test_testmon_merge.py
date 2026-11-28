# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Per-shard testmon maps merge into the one map a full run would have written (#2441 item 9).

A sharded full run writes one ``.testmondata`` per shard, each holding only its
shard's tests. A pull request restores one map and selects against it, so the map
must hold every test. These run a real testmon on a real project, once whole and
once as shards, and require the merge to equal the whole: the same tests, the same
fingerprints, and the same selection after an edit. Anything that could make the
merge wrong refuses instead of guessing.
"""

from __future__ import annotations

import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import textwrap
from pathlib import Path

import pytest

from axiom.extensions.builtins.release.testmon_merge import MergeRefused, merge_maps

SRC = Path(__file__).resolve().parents[5]
PLUGIN = "axiom.extensions.builtins.release.order_plugin"


def _project(root: Path) -> Path:
    root.mkdir(parents=True)
    (root / "pkg").mkdir()
    (root / "pkg" / "__init__.py").write_text("")
    for name in "abcd":
        (root / "pkg" / f"m_{name}.py").write_text(f"def f_{name}(x):\n    return x + 1\n")
        (root / f"test_{name}.py").write_text(
            textwrap.dedent(
                f"""
                from pkg.m_{name} import f_{name}
                def test_one(): assert f_{name}(1) == 2
                def test_two(): assert f_{name}(2) == 3
                """
            )
        )
    (root / "pytest.ini").write_text("[pytest]\naddopts = -m 'not integration'\n")
    return root


def _pytest(cwd: Path, *args: str, shard: str = "") -> subprocess.CompletedProcess:
    env = {
        "PATH": os.environ["PATH"],
        "PYTHONPATH": str(SRC),
        "HOME": str(cwd),
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTEST_ADDOPTS": f"-p {PLUGIN}",
    }
    if shard:
        env["AXIOM_TEST_SHARD"] = shard
    return subprocess.run(
        [sys.executable, "-m", "pytest", "-q", *args],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.fixture(scope="module")
def maps(tmp_path_factory):
    base = tmp_path_factory.mktemp("maps")
    whole, one, two = (_project(base / name) for name in ("whole", "s1", "s2"))
    # One xdist worker, on purpose. Testmon credits a module's import-time lines to the first test
    # that imports it in each *process*, so with several workers two identical whole runs already
    # differ (measured: 81 of 1,102 tests). With one worker the attribution is the same whole or
    # sharded, and the join can be held to exact equality. Not -n 0: without xdist testmon also
    # records the tests a shard deselected, which test_a_sharded_run_with_testmon_needs_xdist covers.
    assert "8 passed" in _pytest(whole, "--testmon-noselect", "-n", "1").stdout
    _pytest(one, "--testmon-noselect", "-n", "1", shard="1/2")
    _pytest(two, "--testmon-noselect", "-n", "1", shard="2/2")
    return base


def _facts(database: Path) -> dict[str, frozenset]:
    """test name -> the (file, content hash, method checksums) it depends on."""
    connection = sqlite3.connect(database)
    try:
        rows = connection.execute(
            """SELECT te.test_name, f.filename, f.fsha, f.method_checksums
               FROM test_execution te
               JOIN test_execution_file_fp l ON l.test_execution_id = te.id
               JOIN file_fp f ON f.id = l.fingerprint_id"""
        ).fetchall()
        names = [r[0] for r in connection.execute("SELECT test_name FROM test_execution")]
    finally:
        connection.close()
    out: dict[str, set] = {name: set() for name in names}
    for name, filename, fsha, blob in rows:
        out[name].add((filename, fsha, bytes(blob or b"")))
    return {k: frozenset(v) for k, v in out.items()}


def test_each_shards_map_holds_only_its_own_tests(maps):
    one, two = _facts(maps / "s1" / ".testmondata"), _facts(maps / "s2" / ".testmondata")
    assert one and two and not set(one) & set(two)


def test_the_merged_map_holds_the_same_tests_and_fingerprints_as_the_whole_run(maps, tmp_path):
    out = tmp_path / ".testmondata"
    merge_maps([maps / "s1" / ".testmondata", maps / "s2" / ".testmondata"], out)
    assert _facts(out) == _facts(maps / "whole" / ".testmondata")
    assert len(_facts(out)) == 8


def test_the_merge_does_not_depend_on_the_order_of_its_inputs(maps, tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    merge_maps([maps / "s1" / ".testmondata", maps / "s2" / ".testmondata"], a)
    merge_maps([maps / "s2" / ".testmondata", maps / "s1" / ".testmondata"], b)
    assert _facts(a) == _facts(b)


def _selected_after_an_edit(project: Path, database: Path) -> dict[str, int]:
    """File -> how many of its tests testmon selects after one source file changes."""
    target = Path(tempfile.mkdtemp()) / "p"
    shutil.copytree(
        project,
        target,
        ignore=shutil.ignore_patterns(".testmondata*", "__pycache__", ".pytest_cache"),
    )
    shutil.copy(database, target / ".testmondata")
    (target / "pkg" / "m_b.py").write_text("def f_b(x):\n    return x + 100\n")
    # one -q is already in _pytest; the second makes collect-only print "file: count"
    out = _pytest(target, "--testmon", "--testmon-forceselect", "--collect-only", "-q").stdout
    found = {}
    for line in out.splitlines():
        name, _, count = line.partition(": ")
        if name.startswith("test_") and count.isdigit():
            found[name] = int(count)
    return found


def test_after_an_edit_the_merged_map_selects_what_the_whole_map_selects(maps, tmp_path):
    out = tmp_path / ".testmondata"
    merge_maps([maps / "s1" / ".testmondata", maps / "s2" / ".testmondata"], out)
    whole = _selected_after_an_edit(maps / "whole", maps / "whole" / ".testmondata")
    merged = _selected_after_an_edit(maps / "whole", out)
    assert whole and set(whole) == {"test_b.py"}, "only the file that uses the edited module"
    assert merged == whole


def test_one_shards_map_alone_would_select_the_other_shards_tests_so_the_merge_matters(maps):
    """Unmerged, a map missing half the suite sends every pull request to the full run."""
    alone = _selected_after_an_edit(maps / "whole", maps / "s1" / ".testmondata")
    assert sum(alone.values()) > sum(
        _selected_after_an_edit(maps / "whole", maps / "whole" / ".testmondata").values()
    )


# --- refusals: a merge that could be wrong is not written ---------------------------------


def _copy(maps, name, tmp_path, label):
    target = tmp_path / label
    shutil.copy(maps / name / ".testmondata", target)
    return target


def test_overlapping_shards_are_refused_and_nothing_is_written(maps, tmp_path):
    out = tmp_path / "out"
    with pytest.raises(MergeRefused, match="both hold"):
        merge_maps([maps / "s1" / ".testmondata", _copy(maps, "s1", tmp_path, "dup")], out)
    assert not out.exists()


def test_maps_from_different_environments_are_refused(maps, tmp_path):
    other = _copy(maps, "s2", tmp_path, "other")
    connection = sqlite3.connect(other)
    connection.execute("UPDATE environment SET python_version = '0.0.0'")
    connection.commit()
    connection.close()
    out = tmp_path / "out"
    with pytest.raises(MergeRefused, match="environment"):
        merge_maps([maps / "s1" / ".testmondata", other], out)
    assert not out.exists()


def test_maps_of_different_testmon_versions_are_refused(maps, tmp_path):
    other = _copy(maps, "s2", tmp_path, "other")
    connection = sqlite3.connect(other)
    connection.execute("PRAGMA user_version = 99")
    connection.commit()
    connection.close()
    with pytest.raises(MergeRefused, match="version"):
        merge_maps([maps / "s1" / ".testmondata", other], tmp_path / "out")


def test_a_missing_or_unreadable_input_is_refused(maps, tmp_path):
    with pytest.raises(MergeRefused):
        merge_maps([maps / "s1" / ".testmondata", tmp_path / "absent"], tmp_path / "out")
    junk = tmp_path / "junk"
    junk.write_text("not a database")
    with pytest.raises(MergeRefused):
        merge_maps([maps / "s1" / ".testmondata", junk], tmp_path / "out")


def test_a_failed_merge_leaves_an_existing_output_untouched(maps, tmp_path):
    out = tmp_path / "out"
    out.write_text("previous map")
    with pytest.raises(MergeRefused):
        merge_maps([maps / "s1" / ".testmondata", tmp_path / "absent"], out)
    assert out.read_text() == "previous map"


def test_no_inputs_is_refused(tmp_path):
    with pytest.raises(MergeRefused):
        merge_maps([], tmp_path / "out")


def test_a_sharded_run_with_testmon_needs_xdist(tmp_path):
    """Without xdist, testmon records the tests a shard deselected too, with no dependencies.
    A map like that must never be written, so the run refuses before it starts."""
    project = _project(tmp_path / "p")
    out = _pytest(project, "--testmon-noselect", "-n", "0", shard="1/2")
    assert out.returncode != 0
    assert "xdist" in out.stdout + out.stderr
    database = project / ".testmondata"
    if database.exists():  # testmon opens its file before the refusal; it must hold no test
        connection = sqlite3.connect(database)
        assert connection.execute("SELECT count(*) FROM test_execution").fetchone()[0] == 0
        connection.close()
    fine = _pytest(project, "-n", "0", shard="1/2")
    assert fine.returncode == 0, "sharding without testmon needs no xdist"
