# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""/where, /status and /model tell the truth about placement and enforce the tier (ADR-168, issue #1136)."""

from __future__ import annotations

import json
import re
from types import SimpleNamespace

import pytest

from axiom.extensions.builtins.chat import where
from axiom.extensions.builtins.chat.builtin_commands import ensure_loaded
from axiom.extensions.builtins.chat.command_registry import REGISTRY, CommandContext
from axiom.infra.orchestrator.session import Session
from tests.test_gateway import _gateway_with_providers, _make_provider

ensure_loaded()


def _strip(s):
    return re.sub(r"\x1b\[[0-9;]*m", "", s)


@pytest.fixture(autouse=True)
def _no_planes(monkeypatch):
    monkeypatch.setattr(where, "_retrieval_where", lambda: where.RetrievalWhere("none", "", ""))


@pytest.fixture
def agent(monkeypatch):
    gw = _gateway_with_providers(
        [
            _make_provider(
                name="home",
                model="small-local",
                endpoint="http://localhost:8080/v1",
                api_key_env="",
                priority=1,
            ),
            _make_provider(
                name="cloudy",
                model="big-cloud",
                endpoint="https://api.anthropic.com/v1",
                routing_tier="public",
                priority=2,
            ),
            _make_provider(
                name="node",
                model="node-model",
                endpoint="https://node.example.edu:3001/v1",
                placement="remote-node",
                node_host="node.example.edu",
                routing_tier="export_controlled",
                priority=3,
            ),
        ],
        monkeypatch,
    )
    return SimpleNamespace(gateway=gw, session=Session(), usage=SimpleNamespace(turns=[]))


def run(agent, line):
    return _strip(
        str(REGISTRY.dispatch(line, CommandContext(agent=agent, store=None, line=line)) or "")
    )


def test_where_prints_the_block(agent):
    out = run(agent, "/where")
    assert "small-local" in out and "LOCAL" in out and "predicted for the next turn" in out


def test_where_json_is_the_mcp_shape(agent):
    d = json.loads(run(agent, "/where --json"))
    assert d["provider"] == "home" and d["placement"] == "local" and d["basis"] == "predicted"


def test_where_after_a_remote_answer_names_the_host(agent):
    agent.gateway.set_provider_override("node")
    agent.gateway._select_provider("chat")
    out = run(agent, "/where")
    assert (
        "REMOTE node.example.edu" in out
        and "answered the last turn" in out
        and "pinned to node" in out
    )


def test_model_lists_every_provider_with_where_it_runs(agent):
    out = run(agent, "/model")
    assert "home" in out and "LOCAL" in out
    assert "cloudy" in out and "CLOUD api.anthropic.com" in out
    assert "node" in out and "REMOTE node.example.edu" in out
    assert "/model <number|name>" in out


def test_model_switches_by_number_and_confirms_where_it_now_runs(agent):
    out = run(agent, "/model 2")
    assert agent.gateway._provider_override == "cloudy"
    assert "Switched to" in out and "CLOUD api.anthropic.com" in out


def test_model_switches_by_name(agent):
    assert "REMOTE node.example.edu" in run(agent, "/model node")
    assert agent.gateway._provider_override == "node"


def test_a_switch_to_a_provider_the_session_content_forbids_is_refused_with_the_alternatives(agent):
    where.note_content_tier(agent.session, "export_controlled")
    out = run(agent, "/model cloudy")
    assert "may not receive export_controlled content" in out and "node" in out
    assert agent.gateway._provider_override is None, "nothing may be stored on a refused switch"


def test_an_admitted_switch_still_works_for_controlled_content(agent):
    where.note_content_tier(agent.session, "export_controlled")
    assert "Switched to" in run(agent, "/model node") and agent.gateway._provider_override == "node"


def test_an_unknown_provider_keeps_the_friendly_error(agent):
    out = run(agent, "/model nope")
    assert "not found" in out.lower() and "home" in out and "node" in out
    assert agent.gateway._provider_override is None


def test_a_number_out_of_range_is_an_error_not_a_crash(agent):
    out = run(agent, "/model 9")
    assert "no provider" in out.lower() or "not found" in out.lower()


def test_status_leads_with_where_the_answer_runs(agent):
    from axiom.extensions.builtins.chat.commands import cmd_status

    agent.session.principal_id = "@p:x"
    out = _strip(
        cmd_status(
            SimpleNamespace(
                gateway=agent.gateway,
                session=agent.session,
                usage=SimpleNamespace(
                    turns=[],
                    total_input_tokens=0,
                    total_output_tokens=0,
                    total_cost=0.0,
                    turn_count=0,
                ),
            )
        )
    )
    assert "Runs" in out and "LOCAL" in out
