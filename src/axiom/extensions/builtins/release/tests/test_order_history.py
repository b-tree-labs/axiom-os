# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""The test history is one compact rolling file that outlives artifact retention (#2441 item 5).

A run records, for every test, its outcome and duration, and the area of the
tree the change touched. The format names tests by opaque strings, so another
language's runner can write the same file later.
"""

from __future__ import annotations

import gzip
import json

from axiom.extensions.builtins.release.order_history import (
    RunRecord,
    append_run,
    flaky_ids,
    group_rows,
    nodeid_from_junit,
    prefixes_of,
    read_history,
    run_from_junit,
)


def _run(run="1", sha="a" * 40, tests=None, **kw):
    return RunRecord(
        run=run,
        sha=sha,
        branch=kw.pop("branch", "main"),
        event=kw.pop("event", "push"),
        at=kw.pop("at", "2026-11-25T00:00:00Z"),
        prefixes=kw.pop("prefixes", ("src/axiom/memory",)),
        tests=tests if tests is not None else [("tests/test_a.py::test_x", "p", 0.25)],
        complete=kw.pop("complete", True),
    )


def test_a_run_round_trips_through_the_file(tmp_path):
    path = tmp_path / "runs.jsonl"
    record = _run(
        tests=[("tests/test_a.py::test_x", "p", 0.25), ("tests/test_a.py::test_y", "f", 1.5)],
        prefixes=("src/axiom/memory", "tests"),
    )
    append_run(path, record)
    (back,) = read_history(path)
    assert back == record


def test_a_gz_path_is_compressed_and_reads_the_same(tmp_path):
    path = tmp_path / "runs.jsonl.gz"
    append_run(path, _run())
    assert gzip.open(path, "rt").readline().startswith('{"k":"ids"')
    assert read_history(path)[0].tests == [("tests/test_a.py::test_x", "p", 0.25)]


def test_each_test_name_is_stored_once_however_many_runs_hold_it(tmp_path):
    path = tmp_path / "runs.jsonl"
    for n in range(5):
        append_run(path, _run(run=str(n)))
    assert path.read_text().count("tests/test_a.py::test_x") == 1


def test_the_file_keeps_only_the_newest_runs(tmp_path):
    path = tmp_path / "runs.jsonl"
    for n in range(7):
        append_run(path, _run(run=str(n)), keep_runs=3)
    assert [r.run for r in read_history(path)] == ["4", "5", "6"]


def test_names_only_old_runs_held_leave_the_dictionary_when_those_runs_do(tmp_path):
    path = tmp_path / "runs.jsonl"
    append_run(path, _run(run="old", tests=[("tests/test_gone.py::test_x", "p", 0.1)]), keep_runs=1)
    append_run(path, _run(run="new"), keep_runs=1)
    assert "test_gone" not in path.read_text()


def test_a_missing_or_corrupt_file_reads_as_no_history_and_never_raises(tmp_path):
    assert read_history(tmp_path / "absent.jsonl") == []
    broken = tmp_path / "broken.jsonl"
    broken.write_text('{"k":"ids","v":1,"ids":["a"]}\nnot json\n{"k":"run"}\n')
    assert read_history(broken) == []
    truncated = tmp_path / "gz.jsonl.gz"
    truncated.write_bytes(b"\x1f\x8b\x08 not really gzip")
    assert read_history(truncated) == []


def test_one_bad_line_does_not_discard_the_runs_around_it(tmp_path):
    path = tmp_path / "runs.jsonl"
    append_run(path, _run(run="1"))
    append_run(path, _run(run="2"))
    lines = path.read_text().splitlines()
    lines.insert(2, "{garbage")
    path.write_text("\n".join(lines) + "\n")
    assert [r.run for r in read_history(path)] == ["1", "2"]


def test_a_newer_format_version_is_ignored_not_misread(tmp_path):
    path = tmp_path / "runs.jsonl"
    append_run(path, _run())
    rewritten = path.read_text().replace('"v":1', '"v":99')
    path.write_text(rewritten)
    assert read_history(path) == []


def test_appending_creates_the_directory(tmp_path):
    path = tmp_path / ".ci-history" / "deep" / "runs.jsonl.gz"
    append_run(path, _run())
    assert path.is_file()


def test_the_columns_stay_aligned_for_a_large_run(tmp_path):
    path = tmp_path / "runs.jsonl"
    tests = [
        (f"tests/test_{i % 50}.py::test_{i}", "f" if i % 977 == 0 else "p", i / 1000)
        for i in range(5000)
    ]
    append_run(path, _run(tests=tests))
    (back,) = read_history(path)
    assert back.tests == tests


def test_durations_keep_millisecond_resolution(tmp_path):
    path = tmp_path / "runs.jsonl"
    append_run(path, _run(tests=[("a::t", "p", 0.0014)]))
    assert read_history(path)[0].tests == [("a::t", "p", 0.001)]


# --- the area a change touched -------------------------------------------------


def test_changed_paths_become_directory_prefixes_of_bounded_depth():
    paths = [
        "src/axiom/extensions/builtins/release/guards.py",
        "src/axiom/extensions/builtins/release/tests/test_x.py",
        "scripts/testmon_blast_radius.py",
        "README.md",
    ]
    assert prefixes_of(paths) == (
        "(root)",
        "scripts",
        "src/axiom/extensions/builtins/release",
    )


def test_a_status_letter_in_a_diff_line_is_not_part_of_the_prefix():
    assert prefixes_of(["M\tsrc/axiom/memory/a.py", "A\tdocs/x/y.md"]) == (
        "docs/x",
        "src/axiom/memory",
    )


def test_no_changed_paths_means_no_prefixes():
    assert prefixes_of([]) == ()


# --- flakes ------------------------------------------------------------------------


def test_a_test_that_failed_and_passed_on_the_same_commit_is_a_flake():
    sha = "b" * 40
    runs = [
        _run("1", sha=sha, tests=[("a::flaky", "f", 1.0), ("a::broken", "f", 1.0)]),
        _run("2", sha=sha, tests=[("a::flaky", "p", 1.0), ("a::broken", "f", 1.0)]),
    ]
    assert flaky_ids(runs) == {"a::flaky"}


def test_a_failure_that_a_later_commit_fixed_is_not_a_flake():
    runs = [
        _run("1", sha="c" * 40, tests=[("a::t", "f", 1.0)]),
        _run("2", sha="d" * 40, tests=[("a::t", "p", 1.0)]),
    ]
    assert flaky_ids(runs) == set()


def test_an_incomplete_run_does_not_make_a_test_look_flaky():
    sha = "e" * 40
    runs = [
        _run("1", sha=sha, tests=[("a::t", "f", 1.0)], complete=False),
        _run("2", sha=sha, tests=[("a::t", "f", 1.0)]),
    ]
    assert flaky_ids(runs) == set()


# --- JUnit results are the history that already exists --------------------------------

_JUNIT = """<?xml version="1.0" encoding="utf-8"?><testsuites name="pytest tests"><testsuite name="pytest" errors="0" failures="1" skipped="1" tests="4" time="1243.804" timestamp="2026-11-25T19:15:25.071664+00:00"><testcase classname="" name="src.axiom.extensions.builtins.secrets.tests.test_the_vault_uses_windows_credential_manager" time="0.000"><skipped message="collection skipped">x</skipped></testcase><testcase classname="tests.test_signal_is_retired" name="test_no_file_says_signal_more_than_it_did" time="2.290"><failure message="AssertionError: retired">tests/test_signal_is_retired.py:91: in test</failure></testcase><testcase classname="tests.infra.test_ci_artifact_retention" name="test_every_upload_sets_retention" time="0.760"></testcase><testcase classname="src.axiom.extensions.builtins.release.tests.test_flake.TestFlake" name="test_a_pass[param-1]" time="0.012"></testcase></testsuite></testsuites>"""


def test_junit_names_become_pytest_node_ids_when_the_file_exists():
    exists = {
        "tests/test_signal_is_retired.py",
        "tests/infra/test_ci_artifact_retention.py",
        "src/axiom/extensions/builtins/release/tests/test_flake.py",
    }.__contains__
    assert (
        nodeid_from_junit("tests.test_signal_is_retired", "test_x", exists)
        == "tests/test_signal_is_retired.py::test_x"
    )
    assert (
        nodeid_from_junit(
            "src.axiom.extensions.builtins.release.tests.test_flake.TestFlake", "test_a[p]", exists
        )
        == "src/axiom/extensions/builtins/release/tests/test_flake.py::TestFlake::test_a[p]"
    )


def test_a_junit_name_with_no_such_file_keeps_its_dotted_name_rather_than_guessing():
    assert nodeid_from_junit("gone.test_x", "test_y", lambda _p: False) == "gone.test_x::test_y"


def test_a_junit_file_becomes_a_run_record():
    record = run_from_junit(
        _JUNIT,
        run="45679303681",
        sha="494d1959f839eb976ad4b23dafa8bc1802fd07f7",
        branch="feat/rivet-c1-trust-rails",
        event="pull_request",
        at="2026-11-25T19:36:16Z",
        changed_paths=["src/axiom/extensions/builtins/release/attempts.py"],
        exists=lambda p: p.endswith(".py"),
    )
    outcomes = {tid: (o, s) for tid, o, s in record.tests}
    assert outcomes[
        "tests/test_signal_is_retired.py::test_no_file_says_signal_more_than_it_did"
    ] == ("f", 2.29)
    assert outcomes[
        "tests/infra/test_ci_artifact_retention.py::test_every_upload_sets_retention"
    ] == ("p", 0.76)
    assert len(record.tests) == 3, "a collection-level skip has no test name and is not a test"
    assert record.prefixes == ("src/axiom/extensions/builtins/release",)
    assert record.complete is True


def test_the_file_is_one_json_object_per_line_a_human_can_grep(tmp_path):
    path = tmp_path / "runs.jsonl"
    append_run(path, _run())
    for line in path.read_text().splitlines():
        assert json.loads(line)["k"] in {"ids", "run"}


# --- rows are stored grouped by module -------------------------------------------------


def test_rows_are_grouped_by_module_in_the_order_each_module_first_finished():
    rows = [
        ("b.py::t1", "p", 1.0),
        ("a.py::t1", "p", 1.0),
        ("b.py::t2", "f", 1.0),
        ("a.py::t2", "p", 1.0),
    ]
    assert [t for t, _o, _s in group_rows(rows)] == ["b.py::t1", "b.py::t2", "a.py::t1", "a.py::t2"]


def test_grouping_keeps_every_row_and_each_modules_own_order():
    rows = [(f"m{i % 3}.py::t{i}", "p", 0.1) for i in range(30)]
    grouped = group_rows(rows)
    assert sorted(grouped) == sorted(rows)
    for m in range(3):
        assert [t for t, _o, _s in grouped if t.startswith(f"m{m}.py")] == [
            t for t, _o, _s in rows if t.startswith(f"m{m}.py")
        ]


def test_a_junit_run_is_stored_grouped():
    xml = (
        "<testsuites><testsuite>"
        '<testcase classname="x.test_b" name="t1" time="1"/>'
        '<testcase classname="x.test_a" name="t1" time="1"/>'
        '<testcase classname="x.test_b" name="t2" time="1"/>'
        "</testsuite></testsuites>"
    )
    record = run_from_junit(
        xml,
        run="1",
        sha="a" * 40,
        branch="main",
        event="push",
        at="t",
        changed_paths=[],
        exists=lambda _p: True,
    )
    assert [t for t, _o, _s in record.tests] == [
        "x/test_b.py::t1",
        "x/test_b.py::t2",
        "x/test_a.py::t1",
    ]
