# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tests for memory wiring on the bare ``axi chat`` agent.

The wiring is responsible for:
  - Looking up the local node identity
  - Building a CompositionService-backed MemoryStack
  - Attaching it to the agent so per-turn fragments persist
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import pytest


@pytest.fixture
def fake_agent():
    """Return an object that quacks enough like ChatAgent for memory wiring."""
    session = SimpleNamespace()  # principal_id will be settable as attr
    return SimpleNamespace(session=session)


def test_attach_memory_is_noop_without_identity(fake_agent):
    """When no node identity exists, the wiring degrades silently."""
    from axiom.extensions.builtins.chat import memory_wiring

    with patch(
        "axiom.vega.federation.identity.load_identity",
        return_value=None,
    ):
        wired = memory_wiring.attach_memory(fake_agent)

    assert wired is False
    assert not hasattr(fake_agent, "_composition")
    assert not hasattr(fake_agent.session, "principal_id")


def test_attach_memory_sets_composition_and_principal(fake_agent, tmp_path, monkeypatch):
    """Identity present → composition wired, principal_id set on session."""
    monkeypatch.setenv("AXIOM_RUNTIME_ROOT", str(tmp_path / "runtime"))

    from axiom.extensions.builtins.chat import memory_wiring

    fake_identity = SimpleNamespace(display_name="ben:laptop", owner="ben@example.com")

    with patch(
        "axiom.vega.federation.identity.load_identity",
        return_value=fake_identity,
    ):
        wired = memory_wiring.attach_memory(fake_agent)

    assert wired is True
    assert hasattr(fake_agent, "_composition")
    assert fake_agent._composition is not None
    # Composition must be the real CompositionService — verify by interface.
    assert hasattr(fake_agent._composition, "write")
    # Principal was normalised to @name:context per feedback_principal_naming.
    assert fake_agent.session.principal_id == "@ben:laptop"


def test_attach_memory_round_trip_writes_a_fragment(fake_agent, tmp_path, monkeypatch):
    """A wired agent's CompositionService can persist a fragment that
    ``axi memory show`` would surface — the contract this whole patch exists
    to deliver."""
    monkeypatch.setenv("AXIOM_RUNTIME_ROOT", str(tmp_path / "runtime"))
    monkeypatch.setattr(
        "axiom.infra.paths.get_user_state_dir", lambda: tmp_path / "state"
    )

    from axiom.extensions.builtins.chat import memory_wiring

    fake_identity = SimpleNamespace(display_name="ben:laptop", owner="ben@example.com")
    with patch(
        "axiom.vega.federation.identity.load_identity",
        return_value=fake_identity,
    ):
        memory_wiring.attach_memory(fake_agent)

    # Now exercise the same write path the chat-turn observability hook uses.
    fragment = fake_agent._composition.write(
        content={"event_time": "2026-04-30T00:00:00Z", "fact_kind": "smoke"},
        cognitive_type="episodic",
        principal_id=fake_agent.session.principal_id,
        agents={"neut-agent"},
        resources={"session:test"},
    )

    assert fragment is not None
    # principal_id lives on the provenance tuple per (T, U, A, R) invariant
    assert fragment.provenance.principal_id == "@ben:laptop"


def test_chat_write_is_visible_to_axi_memory_show(fake_agent, tmp_path, monkeypatch):
    """Contract: a fragment written via the chat-attached composition is
    readable through the same store ``axi memory show`` queries.

    This regression guards against the 0.12.1 issue where chat wrote to
    ``runtime/extensions/chat/`` while the show command read from
    ``get_user_state_dir() / "memory"`` — same code, different SQLite files,
    invisible fragments.
    """
    monkeypatch.setattr(
        "axiom.infra.paths.get_user_state_dir", lambda: tmp_path / "state"
    )

    from axiom.extensions.builtins.chat import memory_wiring

    fake_identity = SimpleNamespace(display_name="ben:laptop", owner="ben@example.com")
    with patch(
        "axiom.vega.federation.identity.load_identity",
        return_value=fake_identity,
    ):
        memory_wiring.attach_memory(fake_agent)

    fake_agent._composition.write(
        content={"event_time": "2026-04-30T00:00:00Z", "fact_kind": "smoke"},
        cognitive_type="episodic",
        principal_id=fake_agent.session.principal_id,
        agents={"neut-agent"},
        resources={"session:test"},
    )

    # Exercise `axi memory show`'s default composition path: both chat-write
    # and show-read MUST resolve to the same artifact registry.
    from axiom.extensions.builtins.memory.cli import _build_default_composition
    from axiom.memory.session_summary import list_fragments_by_principal

    show_composition = _build_default_composition()
    found = list_fragments_by_principal(
        show_composition, "@ben:laptop", limit=10,
    )
    assert len(found) >= 1, (
        "fragment written via chat-composition must be visible via "
        "axi memory show's default composition"
    )
