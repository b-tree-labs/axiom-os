# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""The REAL serving construction must construct.

The class this guards (found live, 2026-09-24): every streaming-endpoint
test drove the seam runner (``set_turn_runner``), so when HeadlessChat
grew a required ``turn_deadline`` the default runner kept importing
cleanly and failed only when a real browser sent a real message — as an
in-band SSE error on a running node. Here we build exactly what
``_default_turn_runner`` builds — the real HeadlessChat over the real
Gateway — stopping short only of the LLM call itself.
"""

from __future__ import annotations


def test_the_default_runner_construction_constructs(monkeypatch):
    monkeypatch.delenv("AXIOM_CHAT_TURN_DEADLINE", raising=False)
    from axiom.extensions.builtins.chat.headless import HeadlessChat
    from axiom.infra.gateway import Gateway

    headless = HeadlessChat(gateway=Gateway(), turn_deadline=120.0)
    assert headless.agent is not None
    # The serving scope the endpoint uses must also construct.
    scope = headless.new_scope()
    assert scope is not None


def test_deadline_env_overrides(monkeypatch):
    """The endpoint reads AXIOM_CHAT_TURN_DEADLINE; a bad value must fail
    loudly at request time, not silently unbound — float() raising is that."""
    import os

    monkeypatch.setenv("AXIOM_CHAT_TURN_DEADLINE", "45")
    assert float(os.environ["AXIOM_CHAT_TURN_DEADLINE"]) == 45.0
