# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Shadow mode: replay the history before the ranker is allowed to run anything (#2441 item 6).

For every recorded run that had failures, rank that run's tests the way the
ranker would have, using only the runs before it, and measure when the first
failure would have been found and how early the failures sit in the order
(APFD, the average percentage of faults detected: 1.0 is every failure first,
0.5 is no better than chance). The ranker goes live only if this shows a gain.
"""

from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest

from axiom.extensions.builtins.release.order_history import RunRecord, append_run, read_history
from axiom.extensions.builtins.release.order_replay import (
    Measure,
    RunReplay,
    apfd,
    first_failure_time,
    main,
    replay,
    summarize,
)

FIXTURE = Path(__file__).parent / "fixtures" / "order_history_real.jsonl"


def _run(n, tests, prefixes=("src/a",), complete=True, sha=None):
    return RunRecord(
        str(n),
        sha or f"{n:040d}",
        "main",
        "push",
        "2026-11-25T00:00:00Z",
        prefixes,
        tests,
        complete,
    )


# --- the two measures ---------------------------------------------------------------


def test_apfd_is_highest_when_the_only_failure_runs_first():
    assert apfd(["a", "b", "c", "d"], {"a"}) == pytest.approx(0.875)
    assert apfd(["a", "b", "c", "d"], {"d"}) == pytest.approx(0.125)


def test_apfd_averages_the_positions_of_every_failure():
    # n=4, m=2, positions 1 and 3: 1 - 4/8 + 1/8
    assert apfd(["a", "b", "c", "d"], {"a", "c"}) == pytest.approx(0.625)


def test_time_to_first_failure_is_the_time_to_finish_the_first_failing_test():
    order = ["a", "b", "c"]
    seconds = {"a": 1.0, "b": 2.0, "c": 4.0}
    assert first_failure_time(order, seconds, {"b", "c"}, workers=1) == pytest.approx(3.0)


def test_with_two_workers_the_first_failure_can_come_from_the_other_worker():
    order = ["a", "b", "c"]
    seconds = {"a": 10.0, "b": 1.0, "c": 4.0}
    # a -> w1 (0..10); b -> w2 (0..1); c -> w2 (1..5). Failing {c}: finishes at 5.
    assert first_failure_time(order, seconds, {"c"}, workers=2) == pytest.approx(5.0)
    assert first_failure_time(order, seconds, {"a", "c"}, workers=2) == pytest.approx(5.0)


# --- replaying ----------------------------------------------------------------------


def _history_with_a_repeat_offender():
    ids = [f"f{k}.py::t{j}" for k in range(10) for j in range(5)]
    runs = []
    for n in range(12):
        tests = [(tid, "f" if tid == "f9.py::t4" and n in (3, 6, 9) else "p", 1.0) for tid in ids]
        runs.append(_run(n, tests))
    # the run to judge: the same test fails again, and sits last in the old order
    runs.append(_run(12, [(tid, "f" if tid == "f9.py::t4" else "p", 1.0) for tid in ids]))
    return runs


def test_a_repeat_offender_is_found_sooner_in_ranked_order_than_in_the_old_order():
    rows = replay(_history_with_a_repeat_offender(), modes=("ranked",), min_history=5)
    (row,) = [r for r in rows if r.run == "12"]
    assert row.failures == 1 and row.failures_seen_before == 1
    assert row.old.ttff == pytest.approx(50.0)
    assert row.modes["ranked"].ttff < 10.0
    assert row.modes["ranked"].apfd > row.old.apfd


def test_a_failure_the_history_never_saw_cannot_be_predicted_and_the_report_says_so():
    runs = _history_with_a_repeat_offender()[:-1]
    ids = [t for t, _o, _s in runs[0].tests]
    runs.append(_run(12, [(tid, "f" if tid == "f4.py::t2" else "p", 1.0) for tid in ids]))
    (row,) = [r for r in replay(runs, modes=("ranked",), min_history=5) if r.run == "12"]
    assert row.failures_seen_before == 0


def test_a_run_too_small_to_have_an_order_is_not_scored():
    runs = [_run(n, [("a.py::t", "p", 1.0)]) for n in range(6)]
    runs.append(_run(6, [("a.py::t", "f", 1.0)]))
    assert replay(runs, modes=("ranked",), min_history=5, min_tests=2) == []
    assert len(replay(runs, modes=("ranked",), min_history=5, min_tests=1)) == 1


def test_runs_with_no_failure_or_that_stopped_early_or_have_too_little_history_are_not_scored():
    runs = [
        _run(0, [("a.py::t", "f", 1.0), ("b.py::t", "p", 1.0)]),
        _run(1, [("a.py::t", "f", 1.0), ("b.py::t", "p", 1.0)]),  # too little history before it
        _run(2, [("a.py::t", "p", 1.0), ("b.py::t", "p", 1.0)]),  # nothing failed
        _run(3, [("a.py::t", "f", 1.0), ("b.py::t", "p", 1.0)], complete=False),
        _run(4, [("a.py::t", "f", 1.0), ("b.py::t", "p", 1.0)]),
    ]
    assert [r.run for r in replay(runs, modes=("ranked",), min_history=3, min_tests=1)] == ["4"]


def test_the_ranker_is_trained_only_on_the_runs_before_the_one_it_is_judged_on():
    """A replay that peeks at the run it scores would show a gain that does not exist."""
    ids = [f"f{k}.py::t" for k in range(6)]
    peeking = [_run(n, [(tid, "p", 1.0) for tid in ids]) for n in range(8)]
    peeking.append(_run(8, [(tid, "f" if tid == "f5.py::t" else "p", 1.0) for tid in ids]))
    (row,) = replay(peeking, modes=("ranked",), min_history=5, min_tests=1)
    assert row.failures_seen_before == 0
    assert row.modes["ranked"].ttff >= 1.0


# --- the verdict ---------------------------------------------------------------------


def test_a_gain_needs_both_measures_better_and_enough_failing_runs_to_say_so():
    rows = replay(_history_with_a_repeat_offender(), modes=("ranked",), min_history=5)
    assert summarize(rows, min_failing_runs=3).gain is True
    unproven = summarize(rows, min_failing_runs=5)
    assert unproven.gain is False
    assert "3 failing runs is too few" in unproven.reason and "at least 5" in unproven.reason


def _row(old_ttff, ranked_ttff, apfd_old=0.4, apfd_ranked=0.6):
    return RunReplay(
        "r",
        "main",
        100,
        1,
        0,
        Measure(old_ttff, old_ttff, apfd_old),
        {"ranked": Measure(ranked_ttff, ranked_ttff, apfd_ranked)},
    )


def test_one_huge_win_cannot_carry_a_mean_over_runs_that_got_worse():
    """The mean of seven runs is the one run that took two hours. Count the runs too."""
    rows = [_row(7000, 100)] + [_row(10, 12) for _ in range(6)]
    summary = summarize(rows, min_failing_runs=1)
    assert (summary.wins, summary.losses) == (1, 6)
    assert summary.gain is False
    assert "1 of 7" in summary.reason


def test_the_default_asks_for_twenty_failing_runs_before_anyone_calls_a_gain():
    rows = [_row(100, 10) for _ in range(19)]
    assert summarize(rows).gain is False
    assert summarize([*rows, _row(100, 10)]).gain is True


def test_no_failing_runs_is_not_a_gain():
    summary = summarize([], min_failing_runs=1)
    assert summary.gain is False and "no failing run" in summary.reason


# --- the command ---------------------------------------------------------------------


def test_the_report_names_the_sample_the_old_and_ranked_orders_and_the_verdict(tmp_path, capsys):
    path = tmp_path / "runs.jsonl"
    for record in _history_with_a_repeat_offender():
        append_run(path, record, keep_runs=100)
    assert main(["replay", str(path), "--min-history", "5", "--min-failing-runs", "3"]) == 0
    out = capsys.readouterr().out
    assert "3 failing runs" in out
    for needle in (
        "time to first failure",
        "APFD",
        "old order",
        "ranked",
        "failed-first",
        "VERDICT",
    ):
        assert needle in out, needle
    # every scored run is listed, so a mean cannot hide that two runs carry it
    assert out.count("run 12 ") == 1 and "run 6 " in out and "run 9 " in out
    assert "gain" in out.split("VERDICT")[1]


def test_an_empty_history_reports_that_and_still_exits_zero(tmp_path, capsys):
    assert main(["replay", str(tmp_path / "none.jsonl")]) == 0
    assert "no history" in capsys.readouterr().out


def test_json_output_carries_the_numbers_for_a_later_decision_record(tmp_path, capsys):
    path = tmp_path / "runs.jsonl"
    for record in _history_with_a_repeat_offender():
        append_run(path, record, keep_runs=100)
    main(["replay", str(path), "--min-history", "5", "--json"])
    data = json.loads(capsys.readouterr().out)
    assert data["failing_runs"] == 3
    assert data["modes"]["ranked"]["mean_ttff"] < data["old"]["mean_ttff"]


# --- history from the JUnit files CI already keeps --------------------------------------


def test_backfill_turns_retained_junit_artifacts_into_history(tmp_path):
    junit = (
        '<testsuites><testsuite><testcase classname="tests.test_x" name="test_a" time="0.5"/>'
        '<testcase classname="tests.test_x" name="test_b" time="0.1"><failure message="m">t</failure></testcase>'
        "</testsuite></testsuites>"
    )
    archive = tmp_path / "1.zip"
    with zipfile.ZipFile(archive, "w") as z:
        z.writestr("junit.xml", junit)
    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps(
            [
                {
                    "run": "1",
                    "zip": str(archive),
                    "sha": "a" * 40,
                    "branch": "main",
                    "event": "push",
                    "at": "2026-11-25T00:00:00Z",
                    "changed": ["src/axiom/a.py"],
                }
            ]
        )
    )
    out = tmp_path / "runs.jsonl.gz"
    assert main(["backfill", str(manifest), "--out", str(out), "--root", str(tmp_path)]) == 0
    (record,) = read_history(out)
    assert [(o) for _t, o, _s in record.tests] == ["p", "f"]
    assert record.prefixes == ("src/axiom",)


# --- the real recorded history -----------------------------------------------------------


def test_sample_keeps_whole_modules_the_modules_that_failed_and_hides_the_branch(tmp_path):
    runs = []
    for n in range(4):
        tests = [
            (f"m{k}.py::t{j}", "f" if (k, j, n) == (7, 1, 2) else "p", 0.123456)
            for k in range(40)
            for j in range(3)
        ]
        runs.append(
            RunRecord(
                str(n), f"{n:040d}", f"feat/secret-name-{n}", "pull_request", "t", ("src/x",), tests
            )
        )
    source = tmp_path / "all.jsonl"
    out = tmp_path / "small.jsonl"
    from axiom.extensions.builtins.release.order_history import write_history

    write_history(source, runs)
    assert main(["sample", str(source), "--out", str(out), "--per-hundred", "10"]) == 0
    small = read_history(out)
    names = {t for r in small for t, _o, _s in r.tests}
    modules = {t.partition("::")[0] for t in names}
    assert "m7.py" in modules, "the module with a failure is always kept"
    assert 2 <= len(modules) < 40
    for module in modules:
        assert sum(1 for t in names if t.startswith(module + "::")) == 3, "a module is kept whole"
    assert all(r.branch in {"main", "pr"} for r in small)
    assert all(abs(s * 100 - round(s * 100)) < 1e-9 for r in small for _t, _o, s in r.tests), (
        "10 ms steps"
    )
    assert [r.tests[0][0].partition("::")[0] for r in small][0] in modules


@pytest.mark.skipif(
    not FIXTURE.exists(), reason="the recorded fixture is built from retained CI artifacts"
)
def test_the_replay_runs_on_the_real_recorded_history_and_reports_honestly(capsys):
    """Real runs of this repository's unit job, 2026-11-22 to 2026-11-25, from the Actions artifacts.

    The fixture is a sample (whole modules, every module that held a failure) so it is small
    enough to commit. This does not assert a gain. It asserts the replay reads real data end to
    end and reports its sample size, so a number cannot be quoted without the sample behind it.
    """
    runs = read_history(FIXTURE)
    assert len(runs) >= 40, "the fixture is the recorded runs, not a sketch"
    assert any(r.event == "pull_request" for r in runs) and any(r.event == "push" for r in runs)
    assert any(o == "f" for r in runs for _t, o, _s in r.tests), "real failures are in it"
    rows = replay(runs, modes=("failed-first", "ranked"))
    assert len(rows) >= 5
    summary = summarize(rows)
    assert summary.failing_runs == len(rows)
    assert summary.old.mean_ttff >= 0
    assert main(["replay", str(FIXTURE)]) == 0
    out = capsys.readouterr().out
    assert f"{len(rows)} failing run" in out and "VERDICT" in out


@pytest.mark.skipif(
    not FIXTURE.exists(), reason="the recorded fixture is built from retained CI artifacts"
)
def test_the_recorded_fixture_is_small_enough_to_commit_and_names_no_branch():
    assert FIXTURE.stat().st_size < 700_000
    assert FIXTURE.read_text().splitlines()[0].startswith('{"k":"ids"')
    assert {r.branch for r in read_history(FIXTURE)} <= {"main", "pr"}
