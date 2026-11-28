# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Run the test most likely to fail, per second it costs, first (#2441 items 3 and 4).

For independent tests, ordering by chance of failing divided by runtime
minimizes the expected time to the first failure. The chance comes from the
recorded history: how often a test failed when files in this area changed,
recent runs counting more, and tests known to flake counting less.
"""

from __future__ import annotations

import random

from axiom.extensions.builtins.release.order_history import RunRecord
from axiom.extensions.builtins.release.order_rank import (
    RankConfig,
    fit,
    group_of,
    order_ids,
)


def _run(n, tests, prefixes=("src/a",), sha=None, complete=True):
    return RunRecord(
        run=str(n),
        sha=sha or f"{n:040d}",
        branch="main",
        event="push",
        at="2026-11-25T00:00:00Z",
        prefixes=prefixes,
        tests=tests,
        complete=complete,
    )


def _order(ids, runs, changed=("src/a",), mode="ranked", **kw):
    return order_ids(ids, runs, changed, mode, **kw)


# --- off, and failed-first -------------------------------------------------------


def test_off_leaves_the_collected_order_alone():
    runs = [_run(1, [("b.py::t", "f", 1.0), ("a.py::t", "p", 1.0)])]
    assert _order(["a.py::t", "b.py::t"], runs, mode="off") == ["a.py::t", "b.py::t"]


def test_a_test_that_failed_last_time_runs_first_and_the_rest_keep_their_order():
    runs = [_run(1, [("a.py::t1", "p", 1.0), ("a.py::t2", "p", 1.0), ("b.py::t3", "f", 1.0)])]
    ids = ["a.py::t1", "a.py::t2", "b.py::t3", "c.py::t4"]
    assert _order(ids, runs, mode="failed-first") == [
        "b.py::t3",
        "a.py::t1",
        "a.py::t2",
        "c.py::t4",
    ]


def test_a_failure_a_later_run_passed_no_longer_runs_first():
    runs = [
        _run(1, [("a.py::t", "f", 1.0)]),
        _run(2, [("a.py::t", "p", 1.0)]),
    ]
    assert _order(["b.py::x", "a.py::t"], runs, mode="failed-first") == ["b.py::x", "a.py::t"]


def test_a_skip_does_not_clear_a_failure():
    runs = [_run(1, [("a.py::t", "f", 1.0)]), _run(2, [("a.py::t", "s", 0.0)])]
    assert _order(["b.py::x", "a.py::t"], runs, mode="failed-first")[0] == "a.py::t"


def test_failed_first_with_no_history_changes_nothing():
    ids = ["b.py::t", "a.py::t"]
    assert _order(ids, [], mode="failed-first") == ids


def test_last_failures_run_cheapest_first():
    runs = [_run(1, [("a.py::slow", "f", 9.0), ("a.py::quick", "f", 0.1)])]
    assert _order(["a.py::slow", "a.py::quick"], runs, mode="failed-first") == [
        "a.py::quick",
        "a.py::slow",
    ]


def test_a_test_that_is_no_longer_collected_is_not_invented():
    runs = [_run(1, [("gone.py::t", "f", 1.0)])]
    assert _order(["a.py::t"], runs, mode="failed-first") == ["a.py::t"]


# --- ranked order -------------------------------------------------------------------


def _history(failures_by_test, runs=30, prefixes_for=None, duration=None):
    """``failures_by_test``: id -> set of run numbers where it failed."""
    duration = duration or {}
    ids = sorted(failures_by_test)
    out = []
    for n in range(runs):
        tests = [
            (tid, "f" if n in failures_by_test[tid] else "p", duration.get(tid, 1.0)) for tid in ids
        ]
        prefixes = prefixes_for(n) if prefixes_for else ("src/a",)
        out.append(_run(n, tests, prefixes))
    return out


def test_a_cheap_test_outranks_an_expensive_one_that_fails_as_often():
    fails = {"a.py::cheap": {3, 9}, "b.py::dear": {3, 9}}
    runs = _history(fails, duration={"a.py::cheap": 0.1, "b.py::dear": 10.0})
    assert _order(["b.py::dear", "a.py::cheap"], runs) == ["a.py::cheap", "b.py::dear"]


def test_the_test_that_fails_when_this_area_changes_runs_before_one_that_fails_elsewhere():
    fails = {"a.py::ours": {2, 4, 6}, "b.py::theirs": {3, 5, 7}}
    runs = _history(fails, prefixes_for=lambda n: ("src/a",) if n in {2, 4, 6} else ("src/b",))
    assert _order(["b.py::theirs", "a.py::ours"], runs, changed=("src/a",)) == [
        "a.py::ours",
        "b.py::theirs",
    ]
    assert _order(["a.py::ours", "b.py::theirs"], runs, changed=("src/b",)) == [
        "b.py::theirs",
        "a.py::ours",
    ]


def test_a_recent_failure_counts_more_than_an_old_one():
    fails = {"a.py::old": {1, 2}, "b.py::recent": {27, 28}}
    runs = _history(fails)
    assert _order(["a.py::old", "b.py::recent"], runs, mode="ranked")[0] == "b.py::recent"


def test_a_known_flake_counts_less_than_a_test_that_fails_for_real():
    sha = "f" * 40
    runs = _history({"a.py::flaky": {5, 6}, "b.py::real": {5, 6}}, runs=10)
    # Make a.py::flaky pass on the same commit that it failed on.
    runs.append(_run(10, [("a.py::flaky", "f", 1.0), ("b.py::real", "p", 1.0)], sha=sha))
    runs.append(_run(11, [("a.py::flaky", "p", 1.0), ("b.py::real", "p", 1.0)], sha=sha))
    assert _order(["a.py::flaky", "b.py::real"], runs)[0] == "b.py::real"


def test_a_test_new_to_the_history_runs_ahead_of_an_equally_cheap_one_that_never_failed():
    runs = _history({"a.py::steady": set()}, runs=20)
    assert _order(["a.py::steady", "b.py::brand_new"], runs)[0] == "b.py::brand_new"


def test_a_file_stays_together_and_in_its_own_order():
    runs = _history({"a.py::t1": set(), "a.py::t2": {4}, "b.py::t1": {5}, "b.py::t2": set()})
    ids = ["a.py::t1", "a.py::t2", "b.py::t1", "b.py::t2"]
    out = _order(ids, runs)
    files = [group_of(tid) for tid in out]
    assert files in (["a.py"] * 2 + ["b.py"] * 2, ["b.py"] * 2 + ["a.py"] * 2)
    for f in ("a.py", "b.py"):
        assert [t for t in out if group_of(t) == f] == [t for t in ids if group_of(t) == f]


def test_the_same_inputs_always_give_the_same_order():
    """xdist workers each order their own copy and must agree."""
    rng = random.Random(7)
    ids = [f"f{rng.randrange(40)}.py::t{i}" for i in range(300)]
    runs = _history(
        {tid: {rng.randrange(30)} for tid in ids[:150]} | {tid: set() for tid in ids[150:]}
    )
    first = _order(ids, runs)
    for _ in range(3):
        assert _order(list(ids), runs) == first


def test_ranking_only_reorders_it_never_drops_or_repeats():
    rng = random.Random(11)
    ids = [f"f{rng.randrange(25)}.py::t{i}" for i in range(500)]
    runs = _history(
        {tid: ({rng.randrange(30)} if i % 7 == 0 else set()) for i, tid in enumerate(ids)}
    )
    for mode in ("off", "failed-first", "ranked"):
        for workers in (0, 4):
            assert sorted(_order(ids, runs, mode=mode, workers=workers)) == sorted(ids)


def test_tests_the_history_never_saw_in_an_incomplete_run_are_not_counted_as_passes():
    runs = [_run(1, [("a.py::t", "f", 1.0)], complete=False)]
    model = fit(runs, ("src/a",), RankConfig())
    assert model.probability("a.py::t") > model.probability("b.py::never_seen") * 0.5
    # a failure in an incomplete run is still a failure.
    assert model.last_failed("a.py::t")


def test_a_corrupt_history_of_nothing_orders_by_cost_alone():
    out = _order(
        ["a.py::slow", "b.py::fast"],
        [_run(1, [("a.py::slow", "p", 9.0), ("b.py::fast", "p", 0.1)])],
    )
    assert out == ["b.py::fast", "a.py::slow"]


# --- parallel workers ---------------------------------------------------------------


def test_with_worksteal_each_workers_first_block_starts_on_a_top_ranked_file():
    """xdist's worksteal hands each worker a contiguous block. Dealing the ranked
    files round-robin puts the six best files first on three workers, not all on one."""
    ids = [f"f{k}.py::t{j}" for k in range(6) for j in range(4)]
    # f0 fails most often ... f5 least.
    fails = {tid: set(range(0, 30, 1 + int(tid[1]))) for tid in ids}
    runs = _history(fails)
    ranked = _order(ids, runs, workers=0)
    dealt = _order(ids, runs, workers=3)
    ranked_files = list(dict.fromkeys(group_of(t) for t in ranked))
    block = len(ids) // 3
    heads = [group_of(dealt[i * block]) for i in range(3)]
    assert heads == ranked_files[:3]
    assert group_of(ranked[0]) == ranked_files[0]
