# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0

"""The presence construction seam (A0) — one place identity/brief/mode bind."""

from __future__ import annotations

from axiom.extensions.builtins.connect.presence import (
    DEFAULT_PRESENCE_BRIEF,
    PresenceConfig,
    build_presence_agent,
    persona_for,
    presence_display_name,
)
from axiom.infra.bus import EventBus
from axiom.infra.orchestrator.session import Session


class _FakeGateway:
    available = True
    providers = ()


class _Settings:
    def __init__(self, name):
        self._name = name

    def get(self, key, default=None):
        return {"user.name": self._name}.get(key, default)


class _Peer:
    display_name = "Alice"


class _Peers:
    def for_context(self, ctx):
        return _Peer() if ctx == "alice" else None


def _agent(cfg=None):
    # Real ChatAgent (cheap to construct, no network) with a fake gateway +
    # in-memory session, so we assert wiring without an LLM or disk session.
    return build_presence_agent(cfg, gateway=_FakeGateway(), bus=EventBus(), session=Session())


def test_persona_switches_neut_when_rag_used():
    assert persona_for(False) == "Axi"
    assert persona_for(True) == "Neut"


def test_default_config_wires_ask_public_and_default_brief():
    a = _agent()
    assert a._interaction_mode == "ask"
    assert a._session_mode == "public"
    assert a._workspace_context == DEFAULT_PRESENCE_BRIEF


def test_config_overrides_brief_and_modes():
    a = _agent(PresenceConfig(brief="BRIEF-X", interaction_mode="agent", session_mode="public"))
    assert a._workspace_context == "BRIEF-X"
    assert a._interaction_mode == "agent"


def test_injected_gateway_is_used():
    g = _FakeGateway()
    a = build_presence_agent(None, gateway=g, bus=EventBus(), session=Session())
    assert a.gateway is g


# --- A1: principal binding + per-user branding --------------------------------

def test_binds_principal_and_human_to_session():
    s = Session()
    a = build_presence_agent(
        None, principal="@axi:bens", accountable_human_id="ben@example.com",
        gateway=_FakeGateway(), bus=EventBus(), session=s,
    )
    assert a.session.principal_id == "@axi:bens"
    assert a.session.accountable_human_id == "ben@example.com"


def test_invalid_principal_handle_rejected():
    import pytest
    with pytest.raises(ValueError):
        build_presence_agent(None, principal="not-a-handle",
                             gateway=_FakeGateway(), bus=EventBus(), session=Session())


def test_branding_self_owned_is_owner_possessive():
    # @axi:bens on this node (local_context defaults to ctx) → local owner name.
    assert presence_display_name("@axi:bens", settings=_Settings("Ben")) == "Ben's Axi"


def test_branding_peer_agent_uses_peer_display_name():
    # @axi:alice from this node ("bens") → remote peer tier → "Alice's Axi".
    out = presence_display_name(
        "@axi:alice", settings=_Settings("Ben"), peers=_Peers(), local_context="bens"
    )
    assert out == "Alice's Axi"


def test_branding_switches_to_neut_when_rag_used():
    assert presence_display_name("@axi:bens", rag_used=True, settings=_Settings("Ben")) == "Ben's Neut"


def test_per_principal_agent_card_has_display_name_and_icon():
    from axiom.vega.federation.agent_card import build_agent_card_for_principal
    card = build_agent_card_for_principal(
        "@axi:bens", display_name="Ben's Axi", icon_url="https://x/avatar.png"
    )
    assert card.name == "Ben's Axi"
    assert card.to_dict()["iconUrl"] == "https://x/avatar.png"


def test_session_round_trips_principal_fields():
    s = Session(principal_id="@axi:bens", accountable_human_id="ben@example.com")
    s.add_message("user", "hi")  # ensure non-empty so fields matter
    d = s.to_dict()
    assert d["principal_id"] == "@axi:bens" and d["accountable_human_id"] == "ben@example.com"
    r = Session.from_dict(d)
    assert r.principal_id == "@axi:bens" and r.accountable_human_id == "ben@example.com"
