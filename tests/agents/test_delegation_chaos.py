# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Delegation under stress, abuse and misbehaviour.

The security tests prove the controls exist. These prove they hold when the
input is hostile, the delegate is broken, and several turns run at once.

Everything here was probed against the real implementation first; each case
that is now asserted is one that behaved badly when measured:

- a 1 MB request sailed through to an LLM turn with no bound;
- a delegate returning ``None`` or ``42`` produced a ``Delegation.answer``
  annotated ``str`` and holding neither, so ``out.answer.strip()`` crashed the
  caller rather than the delegate;
- a 5 MB answer was relayed verbatim;
- a delegate that raised propagated its own exception type, so callers had to
  catch both ``DelegationError`` and whatever the runner felt like.

The parser came through clean and is asserted so it stays that way: no
catastrophic backtracking, homoglyphs and NUL refused.
"""

from __future__ import annotations

import threading
import time

import pytest

from axiom.agents.addressing import NotAddressed, split_handle
from axiom.agents.delegation import (
    MAX_ANSWER_CHARS,
    MAX_REQUEST_CHARS,
    DelegationError,
    delegate,
    delegation_depth,
)


def _agents():
    from axiom.extensions.builtins.connect.agent_router import discover_agents

    return discover_agents()


class _Runner:
    def __init__(self, fn):
        self.fn = fn

    def run(self, request, *, persona, namespace, requester):
        return self.fn(request)


class TestHostileInputToTheParser:
    """Measured, not assumed: each of these completed in under a millisecond."""

    @pytest.mark.parametrize(
        "text",
        [
            "@" + "a" * 50_000,                 # long handle, no separator
            "@" + "a.-" * 16_000,               # the char class that could backtrack
            "@axi" + ":" * 50_000,              # separator flood
            "@" + "a" * 10_000 + " do X",       # long handle, then a real request
        ],
    )
    def test_pathological_input_returns_fast(self, text):
        """A handle parser that can be made to hang is a denial of service on
        every message a harness passes through it."""
        start = time.perf_counter()
        try:
            split_handle(text, known={"axi"})
        except NotAddressed:
            pass
        assert time.perf_counter() - start < 0.5

    @pytest.mark.parametrize(
        "text",
        [
            "@аxi do X",   # Cyrillic а as the first letter
            "@aхi do X",   # Cyrillic х inside
            "@ax\x00i do X",    # NUL in the handle
        ],
    )
    def test_a_lookalike_handle_is_not_the_handle(self, text):
        """`@аxi` must not reach `axi`. A homoglyph that resolved would let a
        crafted string address an agent the reader believes it cannot."""
        with pytest.raises(NotAddressed):
            split_handle(text, known={"axi"})


class TestBoundsOnWhatCrossesTheSeam:
    def test_an_oversized_request_is_refused_before_a_turn_runs(self):
        """1 MB used to reach the model. The cost of answering is unbounded in
        the size of something a caller controls."""
        ran = []
        with pytest.raises(DelegationError, match="too long"):
            delegate(
                "tidy",
                "x" * (MAX_REQUEST_CHARS + 1),
                runner=_Runner(lambda r: ran.append(r) or "ok"),
                agents=_agents(),
            )
        assert ran == [], "refusing after running is worse than not running"

    def test_a_request_at_the_limit_still_runs(self):
        """A bound that also refuses legitimate work is a bound nobody keeps."""
        out = delegate(
            "tidy", "x" * MAX_REQUEST_CHARS, runner=_Runner(lambda r: "ok"), agents=_agents()
        )
        assert out.answer == "ok"

    def test_an_oversized_answer_is_truncated_and_says_so(self):
        """Silently cutting it would publish a partial answer as a whole one."""
        out = delegate(
            "tidy", "x", runner=_Runner(lambda r: "y" * (MAX_ANSWER_CHARS * 2)), agents=_agents()
        )
        assert len(out.answer) <= MAX_ANSWER_CHARS + 200
        assert "truncated" in out.answer.lower()


class TestADelegateThatMisbehaves:
    @pytest.mark.parametrize("bad", [None, 42, [], {"a": 1}])
    def test_a_non_string_answer_does_not_reach_the_caller_as_one(self, bad):
        """`Delegation.answer` is annotated `str`. It held None and 42, so
        `out.answer.strip()` crashed the CALLER — the one place with no
        information about what went wrong."""
        out = delegate("tidy", "x", runner=_Runner(lambda r: bad), agents=_agents())
        assert isinstance(out.answer, str)

    def test_a_raising_delegate_surfaces_as_a_delegation_error(self):
        """One error type, or every caller catches DelegationError AND whatever
        the runner felt like raising."""
        def boom(_r):
            raise RuntimeError("kaboom")

        with pytest.raises(DelegationError, match="kaboom"):
            delegate("tidy", "x", runner=_Runner(boom), agents=_agents())

    def test_a_raising_delegate_still_resets_the_depth(self):
        def boom(_r):
            raise RuntimeError("kaboom")

        with pytest.raises(DelegationError):
            delegate("tidy", "x", runner=_Runner(boom), agents=_agents())
        assert delegation_depth() == 0


class TestConcurrency:
    def test_depth_is_isolated_per_thread(self):
        """Measured: five concurrent delegations each see depth 1. A shared
        counter would refuse the fifth caller for the first four's hops."""
        seen: dict[int, int] = {}

        def worker(n: int) -> None:
            def inner(_r):
                seen[n] = delegation_depth()
                time.sleep(0.01)
                return "ok"

            delegate("tidy", "x", runner=_Runner(inner), agents=_agents())

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert set(seen.values()) == {1}
        assert delegation_depth() == 0


class TestItDoesNotPayTwiceForTheSameAnswer:
    """`discover_agents` walks the builtins tree — about 1.4 ms, and it grows
    with the number of installed extensions. The router discovered the roster
    and then `delegate` discovered it again, so every addressed message paid
    for the same filesystem walk twice to reach an answer that cannot have
    changed between the two calls."""

    def test_an_addressed_message_discovers_the_roster_once(self, monkeypatch):
        from axiom.extensions.builtins.connect import agent_router
        # The function, not the module: the package re-exports `address`, so
        # `from ...skills import address` hands back a callable.
        from axiom.extensions.builtins.agents.skills.address import address

        calls = {"n": 0}
        real = agent_router.discover_agents

        def counted(*a, **k):
            calls["n"] += 1
            return real(*a, **k)

        monkeypatch.setattr(agent_router, "discover_agents", counted)

        # Stub the executor: this measures discovery, not a model turn.
        monkeypatch.setattr(
            "axiom.agents.delegation._HeadlessRunner",
            lambda: type("R", (), {"run": lambda self, r, **k: "ok"})(),
        )
        out = address(message="@tidy what is stale")
        assert out["ok"] is True
        assert calls["n"] == 1, f"the roster was discovered {calls['n']} times"

    def test_the_parser_is_cheap_enough_to_run_on_every_message(self):
        """A harness is told to call this for anything beginning with `@`, so
        the refusal path runs on ordinary conversation too."""
        start = time.perf_counter()
        for _ in range(2_000):
            try:
                split_handle("just talking, not addressed", known={"axi"})
            except NotAddressed:
                pass
        per_call_us = (time.perf_counter() - start) / 2_000 * 1e6
        assert per_call_us < 100, f"{per_call_us:.1f} us per unaddressed message"
