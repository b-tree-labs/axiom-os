# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Eval harness: run a suite of (case, scorer) pairs, return a report."""

from __future__ import annotations


def test_harness_runs_single_case_single_scorer() -> None:
    from axiom.evals import EvalCase, EvalHarness, EvalReport

    harness = EvalHarness()
    case = EvalCase(name="greets", input="hi", expected="hello")

    def exact(output: str, expected: str, **_: object) -> float:
        return 1.0 if output == expected else 0.0

    def runner(inp: str) -> str:
        return "hello"

    report: EvalReport = harness.run(runner=runner, cases=[case], scorers={"exact": exact})

    assert report.total == 1
    assert report.scores["exact"] == 1.0
    assert report.passed is True


def test_harness_aggregates_multiple_cases() -> None:
    from axiom.evals import EvalCase, EvalHarness

    harness = EvalHarness()
    cases = [
        EvalCase(name="a", input="1", expected="1"),
        EvalCase(name="b", input="2", expected="2"),
        EvalCase(name="c", input="3", expected="wrong"),
    ]

    def exact(output: str, expected: str, **_: object) -> float:
        return 1.0 if output == expected else 0.0

    def runner(inp: str) -> str:
        return inp

    report = harness.run(runner=runner, cases=cases, scorers={"exact": exact})

    assert report.total == 3
    assert report.scores["exact"] == 2 / 3
    assert report.passed is False  # default threshold 1.0


def test_harness_respects_threshold() -> None:
    from axiom.evals import EvalCase, EvalHarness

    harness = EvalHarness(thresholds={"exact": 0.5})
    cases = [
        EvalCase(name="a", input="x", expected="x"),
        EvalCase(name="b", input="y", expected="no"),
    ]

    def exact(output: str, expected: str, **_: object) -> float:
        return 1.0 if output == expected else 0.0

    report = harness.run(runner=lambda i: i, cases=cases, scorers={"exact": exact})
    assert report.scores["exact"] == 0.5
    assert report.passed is True


def test_harness_emits_traces_to_provider() -> None:
    from axiom.evals import EvalCase, EvalHarness
    from axiom.infra.tracing import InMemoryTraceProvider

    tracer = InMemoryTraceProvider()
    harness = EvalHarness(trace_provider=tracer)

    cases = [EvalCase(name="a", input="x", expected="x")]

    def exact(output: str, expected: str, **_: object) -> float:
        return 1.0 if output == expected else 0.0

    harness.run(runner=lambda i: i, cases=cases, scorers={"exact": exact})

    # One trace per case, one score per scorer per case.
    assert len(tracer.traces) == 1
    assert tracer.scores[0]["name"] == "exact"
    assert tracer.scores[0]["value"] == 1.0
