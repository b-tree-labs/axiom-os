# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""The Reports-vs-Reality benchmark is honest or it is worthless.

Three properties pinned here:

1. The corpus detectors run against real signal snapshots, and the
   console detector uses the SHIPPED fleet bindings wherever the
   incident maps to a shipped report kind — not a reimplementation
   tuned to win.
2. The benchmark can fail: a genuinely healthy fixture must render
   green under the console detector (a bench whose console column is
   all-catches by construction would be the green-that-cannot-fail
   defect, measured about ourselves).
3. Baselines are given every signal they actually had at the time; the
   console never sees a signal the baselines were denied.
"""

from __future__ import annotations

from axiom.bench.reports_vs_reality import (
    CORPUS,
    HEALTHY_CONTROL,
    BaselineHeartbeatFile,
    BaselineHttpHealth,
    BaselineServiceState,
    ConsoleDetector,
    run_bench,
)


def test_corpus_rows_cite_dated_real_incidents():
    assert len(CORPUS) >= 6
    for row in CORPUS:
        assert row.observed_on, f"{row.name}: every row cites a real date"
        assert row.actually_broken is True


def test_console_catches_every_corpus_row_within_its_bound():
    detector = ConsoleDetector()
    for row in CORPUS:
        verdict = detector.judge(row)
        assert verdict.caught, f"{row.name}: console must refuse green"
        assert verdict.detect_bound_hours is not None
        assert verdict.detect_bound_hours <= 3 * row.cadence_hours


def test_healthy_control_renders_green_the_bench_can_fail():
    verdict = ConsoleDetector().judge(HEALTHY_CONTROL)
    assert not verdict.caught, (
        "a healthy node must NOT be flagged — otherwise the console "
        "column is all-catches by construction and the bench is rigged"
    )


def test_baselines_miss_what_they_historically_missed():
    misses = 0
    for row in CORPUS:
        for baseline in (BaselineServiceState(), BaselineHttpHealth(), BaselineHeartbeatFile()):
            v = baseline.judge(row)
            if row.name in baseline.historically_missed and baseline.applicable(row):
                assert not v.caught, f"{baseline.label} should miss {row.name}"
                misses += 1
    assert misses >= 6, "the corpus must contain the misses that motivated it"


def test_run_bench_emits_a_complete_table():
    table = run_bench()
    assert len(table["rows"]) == len(CORPUS)
    for r in table["rows"]:
        assert set(r) >= {
            "incident", "observed_on", "hidden_for_hours",
            "console_caught", "console_bound_hours", "baseline_caught_by",
            "exposure_note",
        }
    # The headline numbers the case study cites come from here, nowhere else.
    assert table["summary"]["console_catch_rate"] == 1.0
    assert table["summary"]["best_baseline_catch_rate"] < 0.5
    assert table["summary"]["median_hidden_hours"] >= 24
