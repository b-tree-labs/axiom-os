# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Hand a request to a subject-matter-expert agent, and actually run it.

ADR-074 C2 — the line between the router and the runner.

Both halves were already here. ``agent_router`` resolves a name to a persona
and a namespaced toolset; ``HeadlessChat`` answers one request in an isolated,
budgeted scope. Nothing joined them, and ``agent_router``'s own docstring said
so: *"C1 routes; C2 wires the run"*.

Until it was joined, ``delegate_to_agent`` resolved the addressee and returned
the request text back to its caller with a ``delegated_to`` field. That reads
as success. An LLM calling it reported the work as handed off, and nothing had
run — the single most load-bearing place in the system for a declared surface
that does not exist.

What delegation means here, precisely: the SME's OWN persona answers, with its
OWN namespaced tools, in its own scope, and the requester travels with it so
the work has an owner. A caller cannot borrow an agent's name and keep its own
authority — that is why the persona and the namespace are read from the
resolved spec rather than passed in.
"""

from __future__ import annotations

import contextvars
from dataclasses import dataclass
from typing import Any, Protocol


#: How deep a chain of delegations may go. A delegate can itself delegate —
#: that is the point of a team — but `rivet -> tidy -> rivet` is a loop that
#: burns a model budget until something else fails. Two hops covers "ask the
#: expert, who asks the one specialist it needs"; beyond that the chain is far
#: more likely to be a cycle than a plan.
MAX_DELEGATION_DEPTH = 2

#: Longest request that may be handed to a delegate. The cost of answering is
#: unbounded in the size of something the caller controls, and a megabyte of
#: text is a paste accident or an attack long before it is a question.
MAX_REQUEST_CHARS = 20_000

#: Longest answer relayed back. Truncation is MARKED rather than silent: a
#: partial answer presented as a whole one is the failure this bound would
#: otherwise introduce while fixing a cost problem.
MAX_ANSWER_CHARS = 100_000

_DEPTH: contextvars.ContextVar[int] = contextvars.ContextVar("axiom_delegation_depth", default=0)


def delegation_depth() -> int:
    """How many delegations deep the current turn is. 0 when a person asked."""
    return _DEPTH.get()


class DelegationError(Exception):
    """A delegation that could not be made. Carries a did-you-mean when the
    addressee was a near miss, because "unknown agent" without the roster is a
    dead end rather than a correction."""

    def __init__(self, message: str, did_you_mean: list[str] | None = None):
        super().__init__(message)
        self.did_you_mean = did_you_mean or []


@dataclass(frozen=True)
class Delegation:
    """What the SME said, and who said it."""

    agent: str
    answer: str
    namespace: str
    #: Channel-facing name ("TIDY"), which is what callers print. Kept beside
    #: the key rather than derived, so a caller never has to guess the casing.
    display: str = ""


class TurnRunner(Protocol):
    """The executor seam. Injected in tests so they prove the request REACHES
    an executor rather than that a model answered well."""

    def run(self, request: str, *, persona: str, namespace: str, requester: str) -> str: ...


#: Wall-clock seconds one delegated turn may take. `HeadlessChat` requires
#: this and refuses a default, deliberately: "there is no safe default for it".
#: A delegation is a person waiting on an answer from another agent, so the
#: bound is generous enough for a tool-using turn and short enough that a stuck
#: delegate surfaces as a failure rather than a hang.
DELEGATION_TURN_DEADLINE = 120.0


class _HeadlessRunner:
    """The production executor: one bounded turn under the SME's persona.

    Built per delegation rather than shared, because `HeadlessChat.new_scope`
    exists so two requests never share state — a long-lived runner handed
    around would quietly undo that.
    """

    def run(self, request: str, *, persona: str, namespace: str, requester: str) -> str:
        from axiom.extensions.builtins.chat.headless import HeadlessChat
        from axiom.infra.gateway import Gateway

        chat = HeadlessChat(gateway=Gateway(), turn_deadline=DELEGATION_TURN_DEADLINE)
        scope = chat.new_scope()
        # The SME's persona and its namespace ride on the scope; see
        # ChatScope.agent_persona / agent_namespace.
        scope.agent_persona = persona
        scope.agent_namespace = namespace
        scope.agent_requester = requester
        return chat.turn(request, stream=False, scope=scope)


def _as_answer(value: Any) -> str:
    """Whatever the runner returned, as the string the dataclass promises.

    `Delegation.answer` is annotated `str` and held `None` and `42` when a
    runner misbehaved, so `out.answer.strip()` raised in the CALLER — the one
    place with no information about what went wrong. Coercing here keeps the
    contract true at its only boundary.
    """
    text = value if isinstance(value, str) else ("" if value is None else str(value))
    if len(text) <= MAX_ANSWER_CHARS:
        return text
    # Marked, never silent: a partial answer presented as a whole one is worse
    # than the cost problem the bound exists to solve.
    return (
        text[:MAX_ANSWER_CHARS]
        + f"\n\n[truncated: the delegate returned {len(text)} characters, "
        + f"limit {MAX_ANSWER_CHARS}]"
    )


def delegate(
    agent: str,
    request: str,
    *,
    runner: TurnRunner | None = None,
    requester: str = "",
    agents: dict[str, Any] | None = None,
    registry: Any | None = None,
) -> Delegation:
    """Run ``request`` as ``agent``, and return what it said.

    Raises :class:`DelegationError` for an unknown addressee or an empty
    request, in both cases BEFORE anything runs: refusing after running is
    worse than not running, and an empty request burns a turn to say nothing.
    """
    from axiom.extensions.builtins.connect.agent_router import (
        discover_agents,
        resolve_agent,
        suggest,
    )

    if not request or not request.strip():
        raise DelegationError("refusing to delegate an empty request")

    if len(request) > MAX_REQUEST_CHARS:
        # Before anything runs. Refusing after running spends the turn the
        # bound exists to protect.
        raise DelegationError(
            f"request is too long to delegate: {len(request)} characters, "
            f"limit {MAX_REQUEST_CHARS}"
        )

    roster = agents if agents is not None else discover_agents()
    key = (agent or "").strip().lower()
    if key not in roster:
        raise DelegationError(
            f"unknown agent {key!r}; known: {', '.join(sorted(roster))}",
            did_you_mean=suggest(key, set(roster)),
        )

    depth = _DEPTH.get()
    if depth >= MAX_DELEGATION_DEPTH:
        # Refused rather than truncated: a chain this deep has almost certainly
        # cycled, and letting it run one more hop spends a model budget to
        # arrive at the same place.
        raise DelegationError(
            f"delegation refused at depth {depth}: a chain deeper than "
            f"{MAX_DELEGATION_DEPTH} is a loop more often than a plan"
        )

    resolved = resolve_agent(key, roster, registry=registry)
    the_runner = runner if runner is not None else _HeadlessRunner()
    token = _DEPTH.set(depth + 1)
    try:
        answer = the_runner.run(
            request.strip(),
            persona=resolved.persona,
            namespace=resolved.spec.namespace,
            requester=requester,
        )
    except DelegationError:
        raise
    except Exception as exc:  # noqa: BLE001 — one error type for one seam
        # Callers should catch DelegationError, not DelegationError and
        # whatever a runner felt like raising. The original is chained so the
        # traceback still names the real cause.
        raise DelegationError(f"the delegate failed: {type(exc).__name__}: {exc}") from exc
    finally:
        # Reset even on failure: a raised delegation must not leave the counter
        # high and refuse the NEXT unrelated request in this turn.
        _DEPTH.reset(token)
    return Delegation(
        agent=key,
        answer=_as_answer(answer),
        namespace=resolved.spec.namespace,
        display=resolved.spec.display,
    )


__all__ = [
    "MAX_ANSWER_CHARS",
    "MAX_DELEGATION_DEPTH",
    "MAX_REQUEST_CHARS",
    "Delegation",
    "DelegationError",
    "TurnRunner",
    "delegate",
    "delegation_depth",
]
