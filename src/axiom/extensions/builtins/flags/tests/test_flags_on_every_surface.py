# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""One flag, every surface (ADR-056, ADR-210): set at the CLI, resolved the same
over HTTP and MCP; the web's role rules; an audit record on every set."""

from __future__ import annotations

import asyncio
import logging

import pytest

from axiom.extensions.builtins.flags import cli
from axiom.extensions.builtins.flags.skills import bind
from axiom.infra import audit_trail
from axiom.infra.skills import SkillContext, SkillRegistry


@pytest.fixture()
def state(tmp_path, monkeypatch, sqlite_store):
    monkeypatch.setenv("AXI_STATE_DIR", str(tmp_path))
    audit_trail._reset_for_tests()
    yield tmp_path
    audit_trail._reset_for_tests()


def _client(monkeypatch, *, sub="u9", roles=(), site="acct"):
    from fastapi import APIRouter, FastAPI
    from fastapi.testclient import TestClient

    from axiom.extensions.builtins.flags import api

    claims = {"sub": sub, "roles": list(roles), "site": site} if sub else {}
    monkeypatch.setattr(api, "_claims", lambda request: claims)
    router = APIRouter()
    api.register_routes(router)
    app = FastAPI()
    app.include_router(router, prefix="/api/v1")
    return TestClient(app)


def _mcp(tool: str, args: dict, state_dir) -> dict:
    from axiom.extensions.builtins.mcp.skill_tools import skill_tool_contribution

    reg = SkillRegistry()
    bind(reg)
    contrib = skill_tool_contribution(
        reg,
        ctx_factory=lambda: SkillContext(registry=reg, state_dir=state_dir, logger=logging.getLogger("t")),
    )
    return asyncio.run(contrib.dispatch[tool](args))


def _audited(state_dir) -> list[dict]:
    return [r for r in audit_trail.read_chain(audit_trail.audit_path(state_dir)) if r["skill"] == "flags.set"]


def test_a_flag_set_at_the_cli_resolves_the_same_over_http_and_mcp(state, monkeypatch, capsys):
    assert cli.main(["set", "history_timeline", "on"]) == 0
    assert "for node node-a" in capsys.readouterr().out

    web = _client(monkeypatch).get("/api/v1/flags")
    assert web.status_code == 200, web.text
    body = web.json()
    assert body["flags"]["history_timeline"] == "on" and body["sources"]["history_timeline"] == "node"

    mcp = _mcp("axiom_flags__resolve", {"user": "u9", "account": "acct"}, state)
    assert mcp["ok"], mcp
    assert mcp["value"]["flags"] == body["flags"]
    assert mcp["value"]["sources"] == body["sources"]

    assert cli.main(["--json", "resolve", "--user", "u9", "--account", "acct"]) == 0
    import json

    assert json.loads(capsys.readouterr().out)["flags"] == body["flags"]


def test_a_user_sets_their_own_override_on_the_web_and_mcp_sees_it(state, monkeypatch):
    r = _client(monkeypatch).patch("/api/v1/flags/library", json={"value": "on"})
    assert r.status_code == 200, r.text
    assert r.json()["flags"]["library"] == "on" and r.json()["sources"]["library"] == "user"
    mcp = _mcp("axiom_flags__resolve", {"user": "u9", "key": "library"}, state)
    assert mcp["value"]["flags"] == {"library": "on"}
    # someone else is untouched
    assert _mcp("axiom_flags__resolve", {"user": "u8", "key": "library"}, state)["value"]["flags"] == {"library": "soon"}


def test_every_set_writes_an_audit_record_on_every_surface(state, monkeypatch):
    cli.main(["set", "history_timeline", "on"])
    _client(monkeypatch).patch("/api/v1/flags/history_timeline", json={"value": "off"})
    _mcp("axiom_flags__set", {"key": "library", "value": "off", "scope": "account", "scope_id": "acct"}, state)
    cli.main(["clear", "history_timeline"])
    records = _audited(state)
    assert len(records) == 4
    assert {r["outcome"] for r in records} == {"ok"}
    assert records[1]["principal"] == "@u9:acct"
    assert audit_trail.verify_chain(audit_trail.audit_path(state))


def test_a_refused_set_is_audited_as_refused_and_stores_nothing(state, sqlite_store):
    assert cli.main(["set", "no_such_flag", "on"]) == 1
    records = _audited(state)
    assert len(records) == 1 and records[0]["outcome"] != "ok"
    assert sqlite_store.rows() == []


def test_the_stored_row_says_who_set_it(state, monkeypatch, sqlite_store):
    _client(monkeypatch, roles=["operator"]).patch(
        "/api/v1/flags/history_timeline", json={"value": "soon", "scope": "node"})
    (row,) = sqlite_store.rows()
    assert (row["scope"], row["scope_id"], row["updated_by"]) == ("node", "node-a", "@u9:acct")


class TestTheWebRoles:
    def test_signed_out_is_refused(self, state, monkeypatch):
        c = _client(monkeypatch, sub=None)
        assert c.get("/api/v1/flags").status_code == 401
        assert c.patch("/api/v1/flags/library", json={"value": "on"}).status_code == 401

    def test_reading_is_open_to_anyone_signed_in(self, state, monkeypatch):
        body = _client(monkeypatch, roles=["viewer"]).get("/api/v1/flags").json()
        assert body["can_set"] == {"user": True, "account": False, "node": False}
        assert body["metadata"]["beta_planner"]["owner"] == "test"
        assert body["environment"] == "development"

    @pytest.mark.parametrize("roles", [[], ["viewer"], ["owner"]])
    def test_node_scope_needs_the_operator_role(self, state, monkeypatch, roles, sqlite_store):
        r = _client(monkeypatch, roles=roles).patch(
            "/api/v1/flags/history_timeline", json={"value": "on", "scope": "node"})
        assert r.status_code == 403
        assert sqlite_store.rows() == []

    @pytest.mark.parametrize("roles, status", [(["viewer"], 403), (["owner"], 200), (["operator"], 200)])
    def test_account_scope_is_for_owners_and_operators(self, state, monkeypatch, roles, status):
        r = _client(monkeypatch, roles=roles).patch(
            "/api/v1/flags/history_timeline", json={"value": "on", "scope": "account"})
        assert r.status_code == status, r.text

    def test_an_operator_sets_node_scope(self, state, monkeypatch):
        r = _client(monkeypatch, roles=["operator"]).patch(
            "/api/v1/flags/history_timeline", json={"value": "on", "scope": "node"})
        assert r.status_code == 200, r.text
        assert _client(monkeypatch, sub="someone").get("/api/v1/flags").json()["flags"]["history_timeline"] == "on"

    def test_a_protected_flag_is_403_and_an_undeclared_one_404(self, state, monkeypatch):
        monkeypatch.setenv("AXIOM_ENVIRONMENT", "production")
        c = _client(monkeypatch, roles=["operator"])
        assert c.patch("/api/v1/flags/beta_planner", json={"value": "on"}).status_code == 403
        assert c.patch("/api/v1/flags/nope", json={"value": "on"}).status_code == 404
        assert c.patch("/api/v1/flags/library", json={"value": "maybe"}).status_code == 400


def test_the_skills_project_onto_mcp_with_chaining_declared():
    from axiom.extensions.builtins.mcp.skill_tools import is_mcp_exposed

    reg = SkillRegistry()
    bind(reg)
    specs = reg.specs()
    assert {"flags.list", "flags.resolve", "flags.set"} <= set(specs)
    assert all(is_mcp_exposed(specs[n]) for n in ("flags.list", "flags.resolve", "flags.set"))
    assert specs["flags.set"].effective_risk == "reversible-write"
    assert specs["flags.resolve"].effective_risk == "read"
