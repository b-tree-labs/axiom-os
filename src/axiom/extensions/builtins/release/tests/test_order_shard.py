# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Shard the full unit suite without ever losing or repeating a test (#2441 item 9).

The unit job on main, the merge queue and a release is one long run. Split into
shards, each on its own runner, it finishes in a fraction of the time. The split
is by module (a module's tests stay together), balanced by recorded duration, and
a pure function of the module name and a committed table, so every shard computes
the same answer with no shared state to race on. A run that cannot prove the
shards together held every collected test fails.
"""

from __future__ import annotations

import json
import os
import random
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from axiom.extensions.builtins.release.order_history import RunRecord
from axiom.extensions.builtins.release.order_shard import (
    ShardReport,
    assign,
    build_table,
    digest,
    join_fragments,
    load_table,
    main,
    parse_spec,
    save_table,
    select,
    shard_of,
    verify,
)

SRC = Path(__file__).resolve().parents[5]
PLUGIN = "axiom.extensions.builtins.release.order_plugin"


def _ids(modules=40, per=5):
    return [f"m{k}.py::t{j}" for k in range(modules) for j in range(per)]


# --- assignment ----------------------------------------------------------------------


def test_every_module_goes_to_exactly_one_shard():
    table = {f"m{k}.py": float(k + 1) for k in range(30)}
    placed = assign(table, 4)
    assert set(placed) == set(table)
    assert set(placed.values()) <= {0, 1, 2, 3}


def test_the_heaviest_module_leads_and_shards_come_out_within_one_module_of_even():
    table = {f"m{k}.py": float(k + 1) for k in range(60)}
    placed = assign(table, 3)
    loads = [sum(s for m, s in table.items() if placed[m] == i) for i in range(3)]
    assert max(loads) - min(loads) <= max(table.values())


def test_assignment_is_the_same_every_time_so_shards_need_not_talk():
    table = {f"m{k}.py": float((k * 7) % 11 + 1) for k in range(50)}
    assert assign(table, 3) == assign(dict(reversed(list(table.items()))), 3)


def test_a_module_missing_from_the_table_goes_by_a_stable_hash():
    table = {"known.py": 5.0}
    first = shard_of("brand_new.py", assign(table, 4), 4)
    assert first == shard_of("brand_new.py", assign(table, 4), 4)
    assert 0 <= first < 4
    spread = {shard_of(f"new{k}.py", {}, 4) for k in range(100)}
    assert spread == {0, 1, 2, 3}, "new modules spread over every shard"


def test_one_shard_is_the_whole_suite():
    ids = _ids()
    assert select(ids, 0, 1, {}) == ids


# --- the union is the whole suite ------------------------------------------------------


def test_the_shards_together_hold_every_test_once_in_collection_order():
    rng = random.Random(3)
    ids = [f"d{rng.randrange(60)}/m{rng.randrange(200)}.py::t{i}" for i in range(4000)]
    table = {
        f"d{rng.randrange(60)}/m{rng.randrange(200)}.py": rng.random() * 30 for _ in range(150)
    }
    for n in (2, 3, 4, 7):
        parts = [select(ids, i, n, table) for i in range(n)]
        flat = [t for part in parts for t in part]
        assert sorted(flat) == sorted(ids), n
        assert len(flat) == len(set(flat)) == len(ids), n
        for part in parts:
            assert part == [t for t in ids if t in set(part)], "collection order inside a shard"


def test_a_module_is_never_split_across_shards():
    ids = _ids()
    parts = [select(ids, i, 3, {}) for i in range(3)]
    homes = {}
    for index, part in enumerate(parts):
        for test in part:
            homes.setdefault(test.partition("::")[0], set()).add(index)
    assert all(len(v) == 1 for v in homes.values())


def test_the_recorded_suite_splits_into_shards_that_cover_it(tmp_path):
    """The committed real history holds real node ids: every one lands in exactly one shard."""
    from axiom.extensions.builtins.release.order_history import read_history

    fixture = Path(__file__).parent / "fixtures" / "order_history_real.jsonl"
    runs = read_history(fixture)
    ids = [t for t, _o, _s in max(runs, key=lambda r: len(r.tests)).tests]
    assert len(ids) > 300
    table = build_table(runs)
    parts = [select(ids, i, 3, table) for i in range(3)]
    assert sorted(t for p in parts for t in p) == sorted(ids)
    assert all(parts), "no shard is empty"


# --- the table ---------------------------------------------------------------------------


def _run(n, tests, complete=True):
    return RunRecord(str(n), f"{n:040d}", "main", "push", "t", (), tests, complete)


def test_the_table_is_each_modules_mean_seconds_over_recent_complete_runs():
    runs = [
        _run(1, [("a.py::t1", "p", 2.0), ("a.py::t2", "p", 2.0), ("b.py::t", "p", 0.001)]),
        _run(2, [("a.py::t1", "p", 4.0), ("a.py::t2", "p", 4.0), ("b.py::t", "p", 0.001)]),
        _run(3, [("a.py::t1", "p", 99.0)], complete=False),
    ]
    table = build_table(runs, min_seconds=0.5)
    assert table == {"a.py": pytest.approx(6.0)}, "mean of 4 and 8; tiny and incomplete are out"


def test_the_table_round_trips_and_a_bad_file_is_an_empty_table(tmp_path):
    path = tmp_path / "t.json"
    save_table(path, {"a.py": 1.234567, "b.py": 9.0})
    assert load_table(path) == {"a.py": 1.23, "b.py": 9.0}
    assert load_table(tmp_path / "absent.json") == {}
    path.write_text("not json")
    assert load_table(path) == {}


def test_the_table_file_is_stable_text(tmp_path):
    path = tmp_path / "t.json"
    save_table(path, {"b.py": 1.0, "a.py": 2.0})
    assert list(json.loads(path.read_text())["seconds"]) == ["a.py", "b.py"]


# --- the spec fails closed ------------------------------------------------------------------


@pytest.mark.parametrize("spec", ["", "1", "0/3", "4/3", "a/b", "1/0", "-1/3", "1/3/5"])
def test_a_malformed_shard_spec_is_an_error_never_a_full_run(spec):
    with pytest.raises(ValueError):
        parse_spec(spec)


def test_a_good_spec_is_one_based():
    assert parse_spec("1/3") == (0, 3)
    assert parse_spec("3/3") == (2, 3)


# --- proving the union ----------------------------------------------------------------------


def _reports(ids, n, table=None):
    total = digest(ids)
    return [
        ShardReport(i + 1, n, len(ids), total, len(part), digest(part))
        for i in range(n)
        for part in [select(ids, i, n, table or {})]
    ]


def test_verify_accepts_shards_that_together_ran_everything():
    assert verify(_reports(_ids(), 3), expected_shards=3) == []


def test_verify_names_a_missing_shard():
    reports = _reports(_ids(), 3)[:2]
    assert any("shard 3" in p for p in verify(reports, expected_shards=3))


def test_verify_catches_a_test_lost_inside_a_shard():
    reports = _reports(_ids(), 3)
    broken = reports[1]
    reports[1] = ShardReport(
        broken.index,
        broken.count,
        broken.total,
        broken.total_digest,
        broken.selected - 1,
        broken.selected_digest,
    )
    assert verify(reports, expected_shards=3)


def test_verify_catches_a_test_run_twice_even_when_the_counts_still_add_up():
    ids = _ids()
    reports = _reports(ids, 3)
    a, b = reports[0], reports[1]
    # one test moves from shard 2 to shard 1 in the books: counts add up, digests do not
    reports[0] = ShardReport(
        a.index, a.count, a.total, a.total_digest, a.selected + 1, a.selected_digest + 12345
    )
    reports[1] = ShardReport(
        b.index, b.count, b.total, b.total_digest, b.selected - 1, b.selected_digest
    )
    assert verify(reports, expected_shards=3)


def test_verify_catches_shards_that_collected_different_suites():
    reports = _reports(_ids(), 3)
    other = reports[2]
    reports[2] = ShardReport(
        other.index,
        other.count,
        other.total + 1,
        other.total_digest,
        other.selected,
        other.selected_digest,
    )
    assert any("collected" in p for p in verify(reports, expected_shards=3))


def test_verify_catches_a_shard_count_that_disagrees_with_the_workflow():
    assert verify(_reports(_ids(), 3), expected_shards=4)


def test_verify_with_no_reports_is_a_failure():
    assert verify([], expected_shards=3)


# --- the plugin, in a real pytest --------------------------------------------------------------


@pytest.fixture
def project(tmp_path):
    for k in range(6):
        (tmp_path / f"test_m{k}.py").write_text(
            textwrap.dedent(
                f"def test_a(): pass\ndef test_b(): pass\ndef test_c(): assert {k} != 4\n"
            )
        )
    return tmp_path


def _run_pytest(project, *args, env=None):
    full = {
        "PATH": os.environ["PATH"],
        "PYTHONPATH": str(SRC),
        "HOME": str(project),
        "PYTHONDONTWRITEBYTECODE": "1",
    }
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


def _ran(out):
    import re

    return [
        a or b
        for a, b in re.findall(
            r"^\[gw\d+\] \[\s*\d+%\] (?:PASSED|FAILED) (test_\w+\.py::\w+)|^(test_\w+\.py::\w+)\s+(?:PASSED|FAILED)",
            out,
            re.MULTILINE,
        )
    ]


def test_each_shard_runs_its_modules_and_together_they_run_the_suite(project, tmp_path):
    everything = set(_ran(_run_pytest(project).stdout))
    assert len(everything) == 18
    seen = []
    for i in (1, 2, 3):
        report = tmp_path / f"r{i}.json"
        out = _run_pytest(
            project, env={"AXIOM_TEST_SHARD": f"{i}/3", "AXIOM_TEST_SHARD_REPORT": str(report)}
        )
        seen.append(_ran(out.stdout))
        assert json.loads(report.read_text())["index"] == i
    flat = [t for part in seen for t in part]
    assert sorted(flat) == sorted(everything)
    assert len(flat) == len(set(flat))
    reports = [
        ShardReport(**{k: v for k, v in json.loads((tmp_path / f"r{i}.json").read_text()).items()})
        for i in (1, 2, 3)
    ]
    assert verify(reports, expected_shards=3) == []


def test_the_shards_agree_under_xdist_too(project, tmp_path):
    flat = []
    for i in (1, 2):
        out = _run_pytest(
            project, "-n", "2", "--dist=worksteal", env={"AXIOM_TEST_SHARD": f"{i}/2"}
        )
        assert "Different tests were collected" not in out.stdout + out.stderr
        flat += _ran(out.stdout)
    assert len(flat) == len(set(flat)) == 18


def test_a_bad_shard_setting_stops_the_run_rather_than_running_everything(project):
    out = _run_pytest(project, env={"AXIOM_TEST_SHARD": "9/3"})
    assert out.returncode != 0
    assert _ran(out.stdout) == []
    assert "AXIOM_TEST_SHARD" in out.stdout + out.stderr


def test_the_main_command_verifies_report_files(tmp_path, capsys):
    ids = _ids()
    for report in _reports(ids, 2):
        (tmp_path / f"shard-{report.index}.json").write_text(json.dumps(report.__dict__))
    assert (
        main(
            [
                "verify",
                "--shards",
                "2",
                str(tmp_path / "shard-1.json"),
                str(tmp_path / "shard-2.json"),
            ]
        )
        == 0
    )
    assert "every collected test" in capsys.readouterr().out
    assert main(["verify", "--shards", "2", str(tmp_path / "shard-1.json")]) == 1


# --- the shards' history fragments become the run's one record ---------------------------------


def test_fragments_join_into_one_record_with_every_test_once(tmp_path):
    from axiom.extensions.builtins.release.order_history import read_history, write_history

    ids = _ids(modules=6, per=2)
    fragments = []
    for i in range(3):
        part = select(ids, i, 3, {})
        record = RunRecord(
            "9", "a" * 40, "main", "push", "t", ("src/x",), [(t, "p", 0.1) for t in part]
        )
        path = tmp_path / f"f{i}.jsonl"
        write_history(path, [record])
        fragments.append(path)
    out = tmp_path / "history.jsonl.gz"
    assert join_fragments(fragments, out) == len(ids)
    (joined,) = read_history(out)
    assert sorted(t for t, _o, _s in joined.tests) == sorted(ids)
    assert joined.run == "9" and joined.complete is True


def test_one_incomplete_fragment_makes_the_joined_record_incomplete(tmp_path):
    from axiom.extensions.builtins.release.order_history import read_history, write_history

    a = tmp_path / "a.jsonl"
    b = tmp_path / "b.jsonl"
    write_history(a, [RunRecord("1", "s", "main", "push", "t", (), [("m.py::t", "p", 0.1)], True)])
    write_history(b, [RunRecord("1", "s", "main", "push", "t", (), [("n.py::t", "f", 0.1)], False)])
    out = tmp_path / "out.jsonl"
    join_fragments([a, b], out)
    assert read_history(out)[0].complete is False


def test_joining_appends_to_the_history_a_run_restored(tmp_path):
    from axiom.extensions.builtins.release.order_history import (
        append_run,
        read_history,
        write_history,
    )

    out = tmp_path / "history.jsonl"
    append_run(out, RunRecord("old", "s", "main", "push", "t", (), [("m.py::t", "p", 0.1)]))
    fragment = tmp_path / "f.jsonl"
    write_history(
        fragment, [RunRecord("new", "s2", "main", "push", "t", (), [("m.py::t", "f", 0.1)])]
    )
    join_fragments([fragment], out)
    assert [r.run for r in read_history(out)] == ["old", "new"]


def test_a_missing_or_empty_fragment_refuses_so_a_partial_record_is_never_saved(tmp_path):
    from axiom.extensions.builtins.release.order_history import write_history

    good = tmp_path / "g.jsonl"
    write_history(good, [RunRecord("1", "s", "main", "push", "t", (), [("m.py::t", "p", 0.1)])])
    with pytest.raises(ValueError):
        join_fragments([good, tmp_path / "absent.jsonl"], tmp_path / "out.jsonl")
    assert not (tmp_path / "out.jsonl").exists()


def test_the_history_command_joins_fragments(tmp_path, capsys):
    from axiom.extensions.builtins.release.order_history import read_history, write_history

    fragment = tmp_path / "f.jsonl"
    write_history(fragment, [RunRecord("1", "s", "main", "push", "t", (), [("m.py::t", "p", 0.1)])])
    out = tmp_path / "out.jsonl"
    assert main(["history", "--out", str(out), str(fragment)]) == 0
    assert len(read_history(out)) == 1
    assert main(["history", "--out", str(out), str(tmp_path / "absent.jsonl")]) == 1


# --- the committed table -----------------------------------------------------------------------

TABLE = SRC.parent / "scripts" / "unit_shard_durations.json"


def test_the_committed_table_is_built_from_real_runs_and_names_real_modules():
    table = load_table(TABLE)
    assert len(table) > 500, "an empty table would shard by hash alone"
    present = sum(1 for module in table if (SRC.parent / module).is_file())
    assert present / len(table) > 0.95, (
        "the table has drifted from the tree: rebuild it with the table command"
    )


@pytest.mark.parametrize("count", [2, 3, 4, 6])
def test_the_committed_table_balances_the_known_modules_within_five_percent(count):
    table = load_table(TABLE)
    placed = assign(table, count)
    loads = [sum(s for m, s in table.items() if placed[m] == i) for i in range(count)]
    assert max(loads) / min(loads) < 1.05, loads


def test_the_workflow_runs_the_shard_count_the_table_was_checked_for():
    import yaml

    ci = yaml.safe_load((SRC.parent / ".github" / "workflows" / "ci.yml").read_text())
    shards = ci["jobs"]["unit-shards"]["strategy"]["matrix"]["shard"]
    assert len(shards) in (2, 3, 4, 6)
