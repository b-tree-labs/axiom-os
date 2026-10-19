# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Delegate to a subject-matter-expert agent and return what it said.

This extension declared ``# mcp: not-applicable … no agent-invocable
capabilities``, which was true while the only verbs were start/stop/status.
Delegation falsified it: handing a request to the expert that holds the context
is precisely what a consuming harness should be able to do, and until now a
persona could only be reached from inside an interactive chat session.
"""

from __future__ import annotations

from typing import Any


def ask(agent: str = "", request: str = "", **_: Any) -> dict:
    """Ask ``agent`` to do ``request``; return its answer.

    The expert answers under its OWN persona with its OWN namespaced tools, so
    delegating to `rivet` gets RIVET's judgement rather than the caller's
    reasoning wearing RIVET's name. Both are read from the resolved agent and
    never supplied by the caller, so a delegation cannot borrow a name while
    keeping its own authority.

    Neither failure path runs anything: refusing after running is worse than
    not running, and an empty request burns a turn to say nothing.
    """
    from axiom.agents.delegation import DelegationError, delegate

    try:
        out = delegate(agent, request)
    except DelegationError as exc:
        return {"ok": False, "error": str(exc), "did_you_mean": exc.did_you_mean}
    return {"ok": True, "agent": out.display, "answer": out.answer}


__all__ = ["ask"]
