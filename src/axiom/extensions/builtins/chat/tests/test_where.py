# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""The one report behind every "where is this answer coming from?" surface (ADR-168).

The status indicator, `/where`, `/status`, `/model`, the reply stamp and the MCP tool all call
`where_report`; none computes placement itself, so they cannot disagree.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from axiom.extensions.builtins.chat import where
from axiom.infra.orchestrator.session import Session
from tests.test_gateway import _gateway_with_providers, _make_provider


def _agent(monkeypatch, providers=None, session=None):
    providers = providers or [
        _make_provider(
            name="home", endpoint="http://localhost:8080/v1", api_key_env="", priority=1
        ),
        _make_provider(
            name="cloudy",
            endpoint="https://api.anthropic.com/v1",
            routing_tier="public",
            priority=2,
        ),
        _make_provider(
            name="node",
            endpoint="https://node.example.edu:3001/v1",
            placement="remote-node",
            node_host="node.example.edu",
            routing_tier="export_controlled",
            priority=3,
        ),
    ]
    return SimpleNamespace(
        gateway=_gateway_with_providers(providers, monkeypatch), session=session or Session()
    )


@pytest.fixture(autouse=True)
def _no_planes(monkeypatch):
    monkeypatch.setattr(where, "_retrieval_where", lambda: where.RetrievalWhere("none", "", ""))


def test_before_any_turn_it_is_a_prediction_and_says_so(monkeypatch):
    r = where.where_report(_agent(monkeypatch))
    assert r.basis == "predicted" and r.provider == "home" and r.placement.placement == "local"


def test_after_a_turn_it_reports_the_provider_that_actually_answered(monkeypatch):
    a = _agent(monkeypatch)
    a.gateway.set_provider_override("cloudy")
    a.gateway._select_provider("chat")
    r = where.where_report(a)
    assert r.basis == "answered" and r.provider == "cloudy" and r.placement.placement == "cloud"
    assert r.override == "cloudy"


def test_a_remote_node_reports_its_host(monkeypatch):
    a = _agent(monkeypatch)
    a.gateway.set_provider_override("node")
    a.gateway._select_provider("chat")
    r = where.where_report(a)
    assert r.placement.placement == "remote-node" and r.placement.host == "node.example.edu"
    assert "REMOTE node.example.edu" in where.render_stamp(r)


def test_content_tier_is_sticky_once_controlled():
    s = Session()
    assert where.content_tier_of(s) == "any"
    where.note_content_tier(s, "public")
    assert where.content_tier_of(s) == "public"
    where.note_content_tier(s, "export_controlled")
    where.note_content_tier(
        s, "public"
    )  # a later uncontrolled turn does not un-hold the controlled content
    assert where.content_tier_of(s) == "export_controlled"


def test_the_prediction_respects_the_content_tier(monkeypatch):
    a = _agent(monkeypatch)
    where.note_content_tier(a.session, "export_controlled")
    assert where.where_report(a).provider == "node"


def test_no_provider_is_reported_not_invented(monkeypatch):
    a = _agent(monkeypatch, providers=[_make_provider(name="x", api_key_env="MISSING_KEY")])
    r = where.where_report(a)
    assert r.provider == "" and "no provider" in where.render_block(r).lower()


def test_retrieval_is_reported_separately_from_generation(monkeypatch):
    monkeypatch.setattr(
        where,
        "_retrieval_where",
        lambda: where.RetrievalWhere("remote-node", "node.example.edu", "served"),
    )
    r = where.where_report(_agent(monkeypatch))
    assert r.placement.placement == "local" and r.retrieval.kind == "remote-node"
    block = where.render_block(r)
    assert "Retrieval" in block and "node.example.edu" in block


def test_every_surface_prints_the_same_facts(monkeypatch):
    a = _agent(monkeypatch)
    a.gateway.set_provider_override("node")
    a.gateway._select_provider("chat")
    r = where.where_report(a)
    for text in (where.render_block(r), where.render_indicator(r), where.render_stamp(r)):
        assert "node" in text and ("REMOTE" in text)
    d = where.to_dict(r)
    assert (
        d["provider"] == "node"
        and d["placement"] == "remote-node"
        and d["host"] == "node.example.edu"
    )
    json.dumps(d)  # the MCP tool returns it as JSON


def test_an_unverified_placement_is_marked_everywhere(monkeypatch):
    guess = _make_provider(name="mystery", endpoint="https://llm.somewhere.example/v1", priority=1)
    r = where.where_report(_agent(monkeypatch, providers=[guess]))
    assert r.placement.verified is False
    assert "?" in where.render_indicator(r) and where.to_dict(r)["verified"] is False


def test_the_stamp_carries_cost_and_latency_when_known(monkeypatch):
    a = _agent(monkeypatch)
    a.gateway._select_provider("chat")
    s = where.render_stamp(where.where_report(a), cost=0.0032, latency_ms=412)
    assert "$0.003" in s and "412ms" in s and s.startswith("↳")
