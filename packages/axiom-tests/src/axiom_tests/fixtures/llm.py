# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""``mock_llm`` fixture — a canned-response fake LLM for unit tests.

The fake LLM supports two modes:

1. **Sequence mode** — queue of pre-programmed replies; each ``complete``
   call pops the next one. Useful for deterministic scenario tests.
2. **Rule mode** — a callable ``(prompt: str) -> str`` resolves each reply
   dynamically. Useful when tests need to assert on prompt content.

The fixture returns a ``MockLLM`` instance that tests can parametrize via
``fake.queue(...)`` or ``fake.set_rule(...)``.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field

import pytest


@dataclass
class LLMCall:
    """One recorded interaction with the fake LLM."""

    prompt: str
    response: str
    kwargs: dict = field(default_factory=dict)


class MockLLM:
    """A deterministic fake LLM for unit tests.

    Tests populate the fake's queue or rule, then pass the fake into code
    under test. After invocation, tests inspect ``fake.calls`` to assert on
    what the code asked the LLM.
    """

    def __init__(self) -> None:
        self._queue: deque[str] = deque()
        self._rule: Callable[[str], str] | None = None
        self.calls: list[LLMCall] = []

    def queue(self, *responses: str) -> MockLLM:
        """Queue one or more responses; returns ``self`` for chaining."""
        self._queue.extend(responses)
        return self

    def set_rule(self, rule: Callable[[str], str]) -> MockLLM:
        """Set a callable that maps each prompt to its response."""
        self._rule = rule
        return self

    def complete(self, prompt: str, **kwargs: object) -> str:
        """Return a canned reply for ``prompt`` and record the call."""
        if self._rule is not None:
            response = self._rule(prompt)
        elif self._queue:
            response = self._queue.popleft()
        else:
            raise AssertionError(
                "MockLLM has no queued responses and no rule set; "
                "tests must configure it before the code under test runs"
            )
        self.calls.append(LLMCall(prompt=prompt, response=response, kwargs=dict(kwargs)))
        return response

    # ``generate`` is a common alternate alias used by downstream adapters.
    generate = complete

    def reset(self) -> None:
        """Clear queued responses, rule, and recorded calls."""
        self._queue.clear()
        self._rule = None
        self.calls.clear()


@pytest.fixture
def mock_llm() -> MockLLM:
    """Provide a fresh ``MockLLM`` for each test."""
    return MockLLM()


__all__ = ["LLMCall", "MockLLM", "mock_llm"]
