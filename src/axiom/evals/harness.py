# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""EvalHarness — run cases × scorers, produce a report, emit traces."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from axiom.infra.tracing import NullTraceProvider, TraceProvider

Scorer = Callable[..., float]
Runner = Callable[[Any], Any]


@dataclass
class EvalCase:
    name: str
    input: Any
    expected: Any = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class EvalReport:
    total: int
    scores: dict[str, float]  # scorer_name -> mean score
    passed: bool
    per_case: list[dict[str, Any]] = field(default_factory=list)


class EvalHarness:
    """Run a runner over a list of EvalCase, score each, aggregate a report."""

    def __init__(
        self,
        *,
        trace_provider: TraceProvider | None = None,
        thresholds: dict[str, float] | None = None,
    ) -> None:
        self._tracer = trace_provider or NullTraceProvider()
        self._thresholds = thresholds or {}

    def run(
        self,
        *,
        runner: Runner,
        cases: list[EvalCase],
        scorers: dict[str, Scorer],
    ) -> EvalReport:
        per_case: list[dict[str, Any]] = []
        sums: dict[str, float] = {name: 0.0 for name in scorers}

        for case in cases:
            trace_id = self._tracer.start_trace(f"eval:{case.name}", **case.metadata)
            output = runner(case.input)
            self._tracer.log_generation(
                trace_id, model="runner", prompt=case.input, output=output
            )

            case_scores: dict[str, float] = {}
            for sname, scorer in scorers.items():
                value = scorer(output, case.expected, input=case.input)
                case_scores[sname] = value
                sums[sname] += value
                self._tracer.score(trace_id, name=sname, value=value)

            per_case.append({"name": case.name, "output": output, "scores": case_scores})

        n = max(len(cases), 1)
        means = {name: total / n for name, total in sums.items()}

        # A report passes iff every scorer meets its threshold. Default threshold = 1.0.
        passed = all(
            means[name] >= self._thresholds.get(name, 1.0) for name in means
        )

        self._tracer.flush()
        return EvalReport(total=len(cases), scores=means, passed=passed, per_case=per_case)
