# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""ResearchLoop — iterate answer→score→refine until converged or out of budget."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from axiom.infra.tracing import NullTraceProvider, TraceProvider

Runner = Callable[[str, int, list[str]], str]
Scorer = Callable[[str, str], float]
Refiner = Callable[[str, str, float], str]


@dataclass
class ResearchQuestion:
    text: str
    metadata: dict = field(default_factory=dict)


@dataclass
class ResearchResult:
    converged: bool
    iterations: int
    final_answer: str
    final_score: float
    history: list[dict]


class ResearchLoop:
    """Iteratively refine an answer to a question until it meets the score threshold."""

    def __init__(
        self,
        *,
        runner: Runner,
        scorer: Scorer,
        refiner: Refiner,
        threshold: float = 0.9,
        max_iterations: int = 10,
        trace_provider: TraceProvider | None = None,
    ) -> None:
        self._runner = runner
        self._scorer = scorer
        self._refiner = refiner
        self._threshold = threshold
        self._max_iterations = max_iterations
        self._tracer = trace_provider or NullTraceProvider()

    def run(self, question: ResearchQuestion) -> ResearchResult:
        trace_id = self._tracer.start_trace(
            f"research:{question.text[:48]}", **question.metadata
        )

        current_q = question.text
        prior_answers: list[str] = []
        history: list[dict] = []
        answer = ""
        score = 0.0
        converged = False
        i = 0

        for i in range(self._max_iterations):
            answer = self._runner(current_q, i, prior_answers)
            self._tracer.log_generation(
                trace_id,
                model="research-runner",
                prompt=current_q,
                output=answer,
                iteration=i,
            )
            score = self._scorer(answer, current_q)
            self._tracer.score(trace_id, name="research_score", value=score, iteration=i)

            history.append(
                {"iteration": i, "question": current_q, "answer": answer, "score": score}
            )
            prior_answers.append(answer)

            if score >= self._threshold:
                converged = True
                break

            current_q = self._refiner(current_q, answer, score)

        self._tracer.flush()
        return ResearchResult(
            converged=converged,
            iterations=len(history),
            final_answer=answer,
            final_score=score,
            history=history,
        )
