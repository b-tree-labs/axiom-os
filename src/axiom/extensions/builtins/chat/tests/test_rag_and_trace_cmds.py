# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""/rag turns retrieval off for a session; /trace is an admin-only command that non-admins never see."""

from __future__ import annotations

import re
from types import SimpleNamespace

import pytest

from axiom.extensions.builtins.chat import roles, where
from axiom.extensions.builtins.chat.agent import ChatAgent
from axiom.extensions.builtins.chat.builtin_commands import ensure_loaded
from axiom.extensions.builtins.chat.command_registry import NOT_FOUND, REGISTRY, CommandContext
from axiom.extensions.builtins.chat.commands import cmd_help, get_slash_commands
from axiom.infra.orchestrator.session import Session

ensure_loaded()


def _strip(s):
    return re.sub(r"\x1b\[[0-9;]*m", "", str(s))


@pytest.fixture
def admins(monkeypatch):
    """Configure the admin list (the setting) without touching real settings."""
    box = {"value": ["@root:test"]}
    monkeypatch.setattr(roles, "_configured_admins", lambda: box["value"])
    return box


def agent_for(principal):
    a = ChatAgent.__new__(ChatAgent)
    a.session = Session(principal_id=principal)
    a._rag_init_attempted = False
    a._rag_store = object()  # would be queried if /rag off did not stop it
    a.gateway = None
    return a


def run(agent, line, surface="headless"):
    return _strip(
        REGISTRY.dispatch(line, CommandContext(agent=agent, store=None, surface=surface, line=line))
    )


def test_rag_off_stops_retrieval_before_the_store_is_touched():
    a = agent_for("@u:test")
    assert "off" in run(a, "/rag off").lower()
    assert a._rag_context("what is the fuel temperature?") == ""
    assert a.session.context["rag"] == "off"


def test_rag_on_restores_it_and_status_reports_the_state():
    a = agent_for("@u:test")
    run(a, "/rag off")
    assert "off" in run(a, "/rag status").lower()
    assert "on" in run(a, "/rag on").lower()
    assert a.session.context.get("rag") != "off"


def test_rag_off_is_what_the_origin_report_says_not_a_stale_source(monkeypatch):
    monkeypatch.setattr(
        where,
        "_retrieval_where",
        lambda: where.RetrievalWhere("remote-node", "node.example.edu", "x"),
    )
    a = agent_for("@u:test")
    run(a, "/rag off")
    assert where.where_report(a).retrieval.kind == "off"
    assert where._retrieval_label(where.RetrievalWhere("off", "", "")) == "OFF"


def test_trace_is_invisible_to_a_non_admin(admins):
    a = agent_for("@someone:test")
    assert "/trace" not in get_slash_commands(a)
    assert "/trace" not in _strip(cmd_help(a))
    assert "/rag" in get_slash_commands(a)


def test_trace_is_listed_for_an_admin(admins):
    a = agent_for("@root:test")
    assert "/trace" in get_slash_commands(a) and "/trace" in _strip(cmd_help(a))


def test_a_non_admin_who_types_it_anyway_gets_the_unknown_command_path_and_no_change(admins):
    a = agent_for("@someone:test")
    out = REGISTRY.dispatch(
        "/trace on", CommandContext(agent=a, store=None, surface="web", line="/trace on")
    )
    assert out is NOT_FOUND
    assert not a.session.context.get("trace")


def test_an_admin_toggles_trace_and_audit_is_left_alone(admins):
    a = agent_for("@root:test")
    assert "on" in run(a, "/trace on").lower()
    assert a.session.context["trace"] is True
    out = run(a, "/trace off")
    assert (
        "off" in out.lower() and "audit" in out.lower()
    )  # says the routing audit log is not affected
    assert a.session.context["trace"] is False


def test_the_default_listing_without_an_agent_is_the_non_admin_one(admins):
    assert "/trace" not in get_slash_commands()


class TestWhoIsAdmin:
    def test_a_configured_principal_is_admin(self, admins):
        assert roles.is_admin(agent_for("@root:test"), surface="web")
        assert not roles.is_admin(agent_for("@other:test"), surface="web")

    def test_with_no_list_the_person_at_their_own_terminal_is_admin_but_a_served_session_is_not(
        self, admins
    ):
        admins["value"] = []
        assert roles.is_admin(agent_for("@me:test"), surface="repl")
        assert roles.is_admin(agent_for("@me:test"), surface="tui")
        assert not roles.is_admin(agent_for("@me:test"), surface="web")
        assert not roles.is_admin(agent_for("@me:test"), surface="headless")

    def test_a_configured_list_overrides_the_terminal_default(self, admins):
        assert not roles.is_admin(agent_for("@me:test"), surface="repl")

    def test_no_agent_is_never_admin(self, admins):
        assert not roles.is_admin(None, surface="repl")
        assert not roles.is_admin(SimpleNamespace(), surface="repl")


def test_with_trace_on_each_reply_gets_a_trace_block_and_with_it_off_none(monkeypatch):
    from axiom.extensions.builtins.chat.providers.null_render import NullRenderProvider
    from tests.test_gateway import _gateway_with_providers, _make_provider

    monkeypatch.setattr(where, "_retrieval_where", lambda: where.RetrievalWhere("none", "", ""))
    gw = _gateway_with_providers(
        [_make_provider(name="home", endpoint="http://localhost:8080/v1", api_key_env="")],
        monkeypatch,
    )
    gw._select_provider("chat")
    seen = []

    class R(NullRenderProvider):
        def render_message(self, role, content):
            seen.append((role, content))

    a = agent_for("@root:test")
    a.gateway = gw
    a._render = R()
    a.usage = SimpleNamespace(
        turns=[SimpleNamespace(input_tokens=11, output_tokens=22, cost=0.0, latency_ms=412)]
    )
    a._emit_status(routing_tier="public")
    assert seen == []
    a.session.context["trace"] = True
    a._emit_status(routing_tier="public")
    [(role, text)] = seen
    assert role == "system" and "trace" in text.lower()
    assert "home" in text and "11" in text and "22" in text and "public" in text and "412" in text
