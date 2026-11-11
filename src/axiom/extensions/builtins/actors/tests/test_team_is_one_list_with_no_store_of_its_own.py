# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Team (MVP-23): one list of people, agents and nodes, read across where they live.

- MVP-23-A1: people, agents and nodes appear in one list; each row names its kind
  in a word and a shape.
- MVP-23-A2: every agent row names an accountable person; an agent without one is
  listed as a finding.
- MVP-23-A4: no actor data is written by the directory itself (no table, no file).
- MVP-23-A5: a viewer sees only the fields their role may see.
- MVP-10-A1 (Team's share): a person with one tenant sees no other tenant's people
  or counts.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import tomllib
from pathlib import Path

import pytest

pytest.importorskip("fastapi")

from fastapi import APIRouter, FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from axiom.agents.roster import AgentRecord  # noqa: E402
from axiom.infra import site_authority as sa  # noqa: E402
from axiom.infra import tenants  # noqa: E402
from axiom.webauth import (  # noqa: E402
    SESSION_COOKIE,
    User,
    get_password_hash,
    issue_session_token,
    upsert_user_record,
)
from axiom.webauth import keys as keys_mod  # noqa: E402
from axiom.webauth import live  # noqa: E402
from axiom.webauth.api_keys import append_api_key_record, mint_api_key  # noqa: E402

EXT = Path(__file__).resolve().parents[1]


def _agent(handle: str, *, accountable: str = "") -> AgentRecord:
    return AgentRecord(handle=handle, display=handle.upper(), role="steward", domain=f"{handle} work",
                       extension=handle, extensions=(handle,), namespaces=(handle,), asks=(),
                       accountable=accountable)


ROSTER = [_agent("keep"), _agent("rivet", accountable="@ben:site-a"), _agent("tidy")]


@pytest.fixture(autouse=True)
def node(tmp_path, monkeypatch):
    monkeypatch.setenv("ENVIRONMENT", "test")
    monkeypatch.setenv("AXI_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("AXIOM_SITE", "site-a")
    monkeypatch.setenv("AXIOM_SERVED_SITES", "site-a,site-b")
    monkeypatch.setenv("AXIOM_FLEET_NODE_ID", "node-1")
    monkeypatch.delenv("AXIOM_TENANTS_FILE", raising=False)
    monkeypatch.delenv("AXIOM_HOME_SITES", raising=False)
    monkeypatch.delenv("AXIOM_TEAM_STEWARD_ROLES", raising=False)
    monkeypatch.setenv("AXIOM_AGENT_ACCOUNTABLE", "keep=@ana:site-a")
    accounts = tmp_path / "gate-users.json"
    keys = tmp_path / "api-keys.json"
    tuples = tmp_path / "tuples.json"
    monkeypatch.setenv("AXIOM_GATE_USERS_FILE", str(accounts))
    monkeypatch.setenv("AXIOM_GATE_API_KEYS_FILE", str(keys))
    monkeypatch.setenv("AXIOM_DIRECTORY_TUPLE_STORE", f"json:{tuples}")
    pw = get_password_hash("Correct-Horse-9")
    upsert_user_record(accounts, email="ana@example.org", password_hash=pw, name="Ana",
                       roles=["operator"], site="site-a")
    upsert_user_record(accounts, email="sam@example.org", password_hash=pw, name="Sam",
                       roles=["supervisor"], site="site-a")
    upsert_user_record(accounts, email="ro@example.org", password_hash=pw, name="Ro",
                       roles=["reader"], site="site-a")
    upsert_user_record(accounts, email="bea@example.org", password_hash=pw, name="Bea",
                       roles=["reader"], site="site-b")
    token, record = mint_api_key(principal="@ana", scopes=["chat:invoke"], site="site-a")
    append_api_key_record(keys, record)
    tuples.write_text(json.dumps({"tuples": [["user:ro@example.org", "member", "group:shift-2"]]}),
                      encoding="utf-8")
    from axiom.webauth import invitations

    _code, inv = invitations.mint_invitation(principal="@cy", roles=["reader"],
                                             scopes=["chat:invoke"], site="site-a",
                                             expires_in=__import__("datetime").timedelta(days=3))
    invitations.append_invitation(tmp_path / "invitations.json", inv)

    import axiom.agents.roster as roster_mod

    monkeypatch.setattr(roster_mod, "load_roster", lambda **kw: list(ROSTER))
    keys_mod.reset_key_store_for_tests()
    live._reset_for_tests()
    tenants._reset_for_tests()
    sa._reset_default_site_context()
    yield tmp_path
    keys_mod.reset_key_store_for_tests()
    live._reset_for_tests()


def _session(email: str) -> str:
    from axiom.webauth import load_user_records

    rec = next(r for r in load_user_records(live.accounts_path()) if r["email"] == email)
    return issue_session_token(User(**rec), issuer="http://testserver")


def _client(email: str | None) -> TestClient:
    """The served app as ``email`` (their session and its site), through the authz hook."""
    from axiom.extensions.builtins.actors import api
    from axiom.extensions.builtins.authz.decide import decide
    from axiom.extensions.builtins.authz.rules import Rule
    from axiom.extensions.builtins.http.authz_hook import build_authz_hook, build_session_resolver
    from axiom.extensions.builtins.http.middleware import _install_authz
    from axiom.governance.intent import IntentPattern
    from axiom.governance.resource import ResourcePattern

    ctx = sa.build_site_context()
    ctx.add_rule(Rule(name="open_http", intent_pattern=IntentPattern("http.*"), actor_pattern="*",
                      resource_pattern=ResourcePattern("extension://*"), disposition="permit"))
    app = FastAPI()
    _install_authz(app, build_authz_hook(resolve_principal=build_session_resolver(),
                                         decide_fn=lambda env: decide(env, ctx)), lambda: [])
    router = APIRouter(prefix="/api/v1")
    api.register_routes(router)
    app.include_router(router)
    client = TestClient(app)
    if email:
        client.cookies.set(SESSION_COOKIE, _session(email))
    return client


def _list(email: str, **params) -> dict:
    r = _client(email).get("/api/v1/actors", params=params)
    assert r.status_code == 200, r.text
    return r.json()


# ---------------------------------------------------------------------------
# MVP-23-A1
# ---------------------------------------------------------------------------


def test_mvp_23_a1_people_agents_and_nodes_are_one_list_each_naming_its_kind():
    body = _list("ana@example.org")
    kinds = [r["kind"] for r in body["actors"]]
    assert {"person", "agent", "node"} <= set(kinds)
    assert kinds == sorted(kinds, key=["person", "agent", "node"].index)
    for row in body["actors"]:
        assert row["kind_word"] == row["kind"].capitalize()
        assert row["shape"] == {"person": "circle", "agent": "diamond", "node": "square"}[row["kind"]]
        assert row["handle"].startswith("@")
    names = {r["name"] for r in body["actors"]}
    assert {"Ana", "Sam", "Ro", "KEEP", "RIVET", "node-1"} <= names
    invited = [r for r in body["actors"] if r.get("status") == "invited"]
    assert [r["handle"] for r in invited] == ["@cy:site-a"]


def test_mvp_23_a1_filters_by_kind_and_role():
    agents = _list("ana@example.org", kind="agent")["actors"]
    assert {r["kind"] for r in agents} == {"agent"}
    readers = _list("ana@example.org", role="reader")["actors"]
    assert {r["name"] for r in readers} == {"Ro", "cy"}


def test_mvp_23_a1_the_same_list_on_the_cli_and_over_mcp(capsys):
    from axiom.extensions.builtins.actors import cli
    from axiom.extensions.builtins.actors.skills import bind
    from axiom.extensions.builtins.mcp.skill_tools import skill_tool_contribution
    from axiom.infra.skills import SkillContext, SkillRegistry

    assert cli.main(["list", "--json"]) == 0
    at_terminal = json.loads(capsys.readouterr().out)
    reg = SkillRegistry()
    bind(reg)
    tools = skill_tool_contribution(reg, ctx_factory=lambda: SkillContext(
        registry=reg, state_dir=__import__("tempfile").mkdtemp(), logger=logging.getLogger("t")))
    over_mcp = asyncio.run(tools.dispatch["axiom_actors__list"]({}))["value"]
    assert [r["handle"] for r in over_mcp["actors"]] == [r["handle"] for r in at_terminal["actors"]]

    shown = asyncio.run(tools.dispatch["axiom_actors__show"]({"handle": "@rivet"}))
    assert shown["ok"] and shown["value"]["accountable"] == "@ben:site-a"


# ---------------------------------------------------------------------------
# MVP-23-A2
# ---------------------------------------------------------------------------


def test_mvp_23_a2_every_agent_names_an_accountable_person_or_is_a_finding():
    body = _list("ana@example.org", kind="agent")
    by = {r["handle"]: r for r in body["actors"]}
    assert by["@keep"]["accountable"] == "@ana:site-a"      # site configuration
    assert by["@rivet"]["accountable"] == "@ben:site-a"     # the agent's declaration
    assert by["@tidy"]["accountable"] is None and by["@tidy"]["finding"]
    assert body["findings"] == [{"handle": "@tidy", "finding": by["@tidy"]["finding"]}]


def test_mvp_23_a2_a_site_default_covers_every_agent(monkeypatch):
    monkeypatch.setenv("AXIOM_AGENT_ACCOUNTABLE", "*=@ops:site-a")
    body = _list("ana@example.org", kind="agent")
    assert body["findings"] == []
    assert {r["accountable"] for r in body["actors"]} == {"@ops:site-a", "@ben:site-a"}


# ---------------------------------------------------------------------------
# MVP-23-A4
# ---------------------------------------------------------------------------


def _tree(root: Path) -> dict[str, str]:
    return {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(root.rglob("*")) if p.is_file()}


def test_mvp_23_a4_the_directory_writes_nothing_and_opens_no_database(node, monkeypatch):
    import axiom.infra.db as db

    def no_database(*a, **kw):
        raise AssertionError("the directory of actors opened a database session")

    monkeypatch.setattr(db, "session_for", no_database)
    before = _tree(node)
    _list("ana@example.org")
    _client("ana@example.org").get("/api/v1/actors/@rivet")
    from axiom.extensions.builtins.actors import cli

    cli.main(["list", "--json"])
    assert _tree(node) == before


def test_mvp_23_a4_the_extension_declares_no_table():
    manifest = tomllib.loads((EXT / "axiom-extension.toml").read_text())
    assert "database" not in manifest
    assert not (EXT / "migrations").exists() and not (EXT / "db_models.py").exists()
    sources = "\n".join(p.read_text() for p in EXT.rglob("*.py") if "tests" not in p.parts)
    for word in ("session_for", "create_all", "CREATE TABLE", "declarative_base"):
        assert word not in sources


# ---------------------------------------------------------------------------
# MVP-23-A5
# ---------------------------------------------------------------------------


def _person(body: dict, name: str) -> dict:
    return next(r for r in body["actors"] if r.get("name") == name)


def test_mvp_23_a5_a_member_sees_name_roles_and_contact_buttons_only():
    ro = _person(_list("ro@example.org"), "Ana")
    assert {"name", "handle", "roles", "contact", "kind"} <= set(ro)
    for hidden in ("email", "keys", "status", "groups", "user_id", "invitation"):
        assert hidden not in ro
    assert "ana@example.org" not in json.dumps(_list("ro@example.org"))


def test_mvp_23_a5_a_steward_also_sees_status_and_groups_not_the_address():
    body = _list("sam@example.org")
    ro = _person(body, "Ro")
    assert ro["status"] == "active" and ro["groups"] == ["shift-2"]
    assert "email" not in ro and "keys" not in ro


def test_mvp_23_a5_an_administrator_sees_the_address_keys_and_invitations():
    body = _list("ana@example.org")
    ana = _person(body, "Ana")
    assert ana["email"] == "ana@example.org" and ana["keys"]["active"] == 1
    cy = next(r for r in body["actors"] if r.get("status") == "invited")
    assert cy["invitation"]["expires_at"]
    assert body["can"] == {"invite": True, "set_roles": True, "suspend": True}
    assert "reader" in body["roles_defined"]


def test_mvp_23_a5_the_steward_roles_are_site_configuration(monkeypatch):
    monkeypatch.setenv("AXIOM_TEAM_STEWARD_ROLES", "shift-lead")
    assert "status" not in _person(_list("sam@example.org"), "Ro")


def test_mvp_23_a5_signed_out_sees_nothing():
    assert _client(None).get("/api/v1/actors").status_code in (401, 403)


# ---------------------------------------------------------------------------
# MVP-10-A1 for Team
# ---------------------------------------------------------------------------


def test_mvp_10_a1_a_tenant_sees_none_of_another_tenants_people_or_counts():
    body = _list("ro@example.org")
    assert "Bea" not in {r["name"] for r in body["actors"]}
    assert body["counts"]["person"] == 4            # Ana, Sam, Ro and the invited cy
    assert "site-b" not in json.dumps(body)
    hidden = _client("ro@example.org").get("/api/v1/actors/@bea:site-b")
    absent = _client("ro@example.org").get("/api/v1/actors/@nobody:site-a")
    assert hidden.status_code == absent.status_code == 404


# ---------------------------------------------------------------------------
# Admin doors: the gate's verbs, scoped to the admin's site
# ---------------------------------------------------------------------------


def test_an_administrator_invites_and_sets_a_role_from_team():
    admin = _client("ana@example.org")
    r = admin.post("/api/v1/actors/invite", json={"handle": "@dee", "name": "Dee", "role": "reader"})
    assert r.status_code == 200, r.text
    assert r.json()["value"]["principal"] == "@dee:site-a" and r.json()["value"]["code"]

    r = admin.post("/api/v1/actors/roles", json={"email": "ro@example.org", "set": ["contributor"]})
    assert r.status_code == 200, r.text
    assert _person(_list("ana@example.org"), "Ro")["roles"] == ["contributor"]


def test_a_member_cannot_invite_or_change_roles():
    member = _client("ro@example.org")
    assert member.post("/api/v1/actors/invite", json={"handle": "@x", "role": "reader"}).status_code == 403
    assert member.post("/api/v1/actors/roles",
                       json={"email": "ana@example.org", "set": ["reader"]}).status_code == 403


def test_a_site_administrator_cannot_touch_another_sites_account():
    admin = _client("ana@example.org")
    assert admin.post("/api/v1/actors/roles",
                      json={"email": "bea@example.org", "set": ["operator"]}).status_code == 404
    assert admin.post("/api/v1/actors/invite",
                      json={"handle": "@x:site-b", "role": "reader"}).status_code == 403


def test_suspend_from_team_ends_the_session_and_resume_restores_it():
    admin = _client("ana@example.org")
    ro = _client("ro@example.org")
    assert ro.get("/api/v1/actors").status_code == 200
    assert admin.post("/api/v1/actors/suspend", json={"email": "ro@example.org"}).status_code == 200
    assert ro.get("/api/v1/actors").status_code in (401, 403)
    assert _person(_list("ana@example.org"), "Ro")["status"] == "suspended"
    assert admin.post("/api/v1/actors/reinstate", json={"email": "ro@example.org"}).status_code == 200
    assert ro.get("/api/v1/actors").status_code == 200
