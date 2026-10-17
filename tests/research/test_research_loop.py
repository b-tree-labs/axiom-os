# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""ResearchLoop — Karpathy-style iterate until convergence or budget exhausted."""

from __future__ import annotations


def test_loop_converges_when_score_meets_threshold() -> None:
    from axiom.research import ResearchLoop, ResearchQuestion

    scores = iter([0.3, 0.6, 0.95])

    def runner(question: str, iteration: int, prior: list[str]) -> str:
        return f"answer-{iteration}"

    def scorer(answer: str, question: str) -> float:
        return next(scores)

    def refiner(question: str, answer: str, score: float) -> str:
        return question + " [refined]"

    loop = ResearchLoop(
        runner=runner, scorer=scorer, refiner=refiner, threshold=0.9, max_iterations=10
    )
    result = loop.run(ResearchQuestion(text="what is X?"))

    assert result.converged is True
    assert result.iterations == 3
    assert result.final_score == 0.95
    assert result.final_answer == "answer-2"


def test_loop_halts_at_max_iterations() -> None:
    from axiom.research import ResearchLoop, ResearchQuestion

    def runner(question: str, iteration: int, prior: list[str]) -> str:
        return "meh"

    def scorer(answer: str, question: str) -> float:
        return 0.1

    loop = ResearchLoop(
        runner=runner,
        scorer=scorer,
        refiner=lambda q, a, s: q,
        threshold=0.9,
        max_iterations=3,
    )
    result = loop.run(ResearchQuestion(text="unanswerable"))

    assert result.converged is False
    assert result.iterations == 3
    assert result.final_score == 0.1


def test_loop_refines_question_between_iterations() -> None:
    from axiom.research import ResearchLoop, ResearchQuestion

    seen_questions: list[str] = []

    def runner(question: str, iteration: int, prior: list[str]) -> str:
        seen_questions.append(question)
        return "a"

    scores = iter([0.2, 0.5, 0.99])

    loop = ResearchLoop(
        runner=runner,
        scorer=lambda a, q: next(scores),
        refiner=lambda q, a, s: q + f" +hint{len(seen_questions)}",
        threshold=0.9,
        max_iterations=5,
    )
    loop.run(ResearchQuestion(text="Q0"))

    assert seen_questions[0] == "Q0"
    assert seen_questions[1] == "Q0 +hint1"
    assert seen_questions[2] == "Q0 +hint1 +hint2"


def test_loop_emits_traces_per_iteration() -> None:
    from axiom.infra.tracing import InMemoryTraceProvider
    from axiom.research import ResearchLoop, ResearchQuestion

    tracer = InMemoryTraceProvider()
    scores = iter([0.4, 0.95])

    loop = ResearchLoop(
        runner=lambda q, i, p: "a",
        scorer=lambda a, q: next(scores),
        refiner=lambda q, a, s: q,
        threshold=0.9,
        max_iterations=5,
        trace_provider=tracer,
    )
    loop.run(ResearchQuestion(text="Q"))

    assert len(tracer.traces) == 1
    assert tracer.traces[0]["name"].startswith("research:")
    # One generation per iteration, one score per iteration.
    assert len(tracer.generations) == 2
    assert len(tracer.scores) == 2
