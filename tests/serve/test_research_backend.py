# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""research_loop_backend — adapt a ResearchLoop into a chat-completions backend.

Open WebUI (and any OpenAI client) can then drive the iterative research
loop as if it were a chat model. Last user message becomes the research
question; the loop's final answer is the assistant reply.
"""

from __future__ import annotations


def test_backend_routes_last_user_message_into_loop() -> None:
    from axiom.research import ResearchLoop
    from axiom.serve import ChatCompletionsHandler
    from axiom.serve.research_backend import research_loop_backend

    seen_questions: list[str] = []
    scores = iter([0.95])

    def runner(q, i, prior):
        seen_questions.append(q)
        return f"A: {q}"

    loop = ResearchLoop(
        runner=runner,
        scorer=lambda a, q: next(scores),
        refiner=lambda q, a, s: q,
        threshold=0.9,
        max_iterations=3,
    )

    backend = research_loop_backend(loop)
    h = ChatCompletionsHandler(backend=backend)

    resp = h.handle(
        {
            "model": "axiom-research",
            "messages": [
                {"role": "system", "content": "you are CURIO"},
                {"role": "user", "content": "what is Xe-135?"},
            ],
        }
    )

    assert seen_questions == ["what is Xe-135?"]
    assert resp["choices"][0]["message"]["content"] == "A: what is Xe-135?"


def test_backend_errors_when_no_user_message() -> None:
    import pytest

    from axiom.research import ResearchLoop
    from axiom.serve.research_backend import research_loop_backend

    loop = ResearchLoop(
        runner=lambda q, i, p: "a",
        scorer=lambda a, q: 1.0,
        refiner=lambda q, a, s: q,
    )
    backend = research_loop_backend(loop)
    with pytest.raises(ValueError, match="no user message"):
        backend([{"role": "system", "content": "x"}], model="m")


def test_backend_uses_latest_user_message_when_multiple() -> None:
    from axiom.research import ResearchLoop
    from axiom.serve.research_backend import research_loop_backend

    seen: list[str] = []

    def runner(q, i, prior):
        seen.append(q)
        return "ok"

    loop = ResearchLoop(
        runner=runner,
        scorer=lambda a, q: 1.0,
        refiner=lambda q, a, s: q,
        threshold=0.5,
        max_iterations=2,
    )
    backend = research_loop_backend(loop)
    backend(
        [
            {"role": "user", "content": "first"},
            {"role": "assistant", "content": "..."},
            {"role": "user", "content": "second"},
        ],
        model="m",
    )
    assert seen[0] == "second"
