# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Agents on Team: active ones only, each with the person who answers for it.

- TEAM-AG-1: a fresh site's first administrator account starts the site's record:
  the assistant is active and that account answers for it; every other agent type
  is dormant, off Team, with no "no accountable person" line anywhere.
- TEAM-AG-2: adding an agent type puts it on Team, and the person adding it answers
  for it; a person named in site configuration or the agent's declaration is kept.
- TEAM-AG-3: site start is idempotent and writes one audit entry per change; an
  existing site's active agent with nobody gets the oldest site administrator.
- TEAM-AG-4: an accountable person who is suspended or removed shows as an issue
  with its fix (assign someone), and assigning someone clears it.
- TEAM-AG-5: an administrator deactivates an agent, which returns it to dormant
  with its record kept; a dormant agent takes no requests; members change nothing.
- TEAM-AG-6: dormancy leaves the roster whole, so an agent is still never taken
  for a person (approvals, GUARD).
"""

from __future__ import annotations

import json
import logging

import pytest

pytest.importorskip("fastapi")

from fastapi import APIRouter, FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from axiom.agents import activation  # noqa: E402
from axiom.agents.roster import AgentRecord  # noqa: E402
from axiom.infra import (
    audit_trail,  # noqa: E402
    tenants,  # noqa: E402
)
from axiom.infra import site_authority as sa  # noqa: E402
from axiom.webauth import (  # noqa: E402
    SESSION_COOKIE,
    User,
    get_password_hash,
    issue_session_token,
    live,  # noqa: E402
    load_user_records,
    upsert_user_record,
)


def _agent(handle: str, *, role: str = "steward", accountable: str = "") -> AgentRecord:
    return AgentRecord(handle=handle, display=handle.upper() if handle != "axi" else "Axi", role=role,
                       domain=f"{handle} work", extension=handle, extensions=(handle,),
                       namespaces=(handle,), asks=(f"ask {handle}",), accountable=accountable)


ROSTER = [_agent("axi", role="the assistant"), _agent("keep"), _agent("clerk"),
          _agent("rivet", accountable="@ben:site-a"), _agent("tidy")]
PW = "Correct-Horse-9"


@pytest.fixture(autouse=True)
def site(tmp_path, monkeypatch):
    monkeypatch.setenv("ENVIRONMENT", "test")
    monkeypatch.setenv("AXI_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("AXIOM_SITE", "site-a")
    monkeypatch.setenv("AXIOM_SERVED_SITES", "site-a")
    monkeypatch.setenv("AXIOM_FLEET_NODE_ID", "node-1")
    for var in ("AXIOM_TENANTS_FILE", "AXIOM_HOME_SITES", "AXIOM_AGENT_ACCOUNTABLE",
                "AXIOM_GATE_API_KEYS_FILE", "AXIOM_DIRECTORY_TUPLE_STORE", "AXIOM_TEAM_STEWARD_ROLES"):
        monkeypatch.delenv(var, raising=False)
    accounts = tmp_path / "gate" / "users.json"
    accounts.parent.mkdir()
    monkeypatch.setenv("AXIOM_GATE_USERS_FILE", str(accounts))

    import axiom.agents.roster as roster_mod
    import axiom.extensions.builtins.connect.agent_router as router_mod

    monkeypatch.setattr(roster_mod, "load_roster", lambda **kw: list(ROSTER))
    monkeypatch.setattr(router_mod, "discover_agents", lambda **kw: {a.handle: a for a in ROSTER})
    live._reset_for_tests()
    tenants._reset_for_tests()
    sa._reset_default_site_context()
    yield tmp_path
    live._reset_for_tests()


def _add_account(email: str, name: str, roles: list[str]) -> None:
    upsert_user_record(live.accounts_path(), email=email, password_hash=get_password_hash(PW),
                       name=name, roles=roles, site="site-a")


def _first_account(email: str = "ana@example.org", name: str = "Ana") -> None:
    """The bootstrap path: the gate's own verb adding the site's first administrator."""
    from axiom.extensions.builtins.webgate.skills import adduser
    from axiom.infra.skills import SkillContext, SkillRegistry

    ctx = SkillContext(registry=SkillRegistry(), state_dir=live.accounts_path().parent,
                       logger=logging.getLogger("t"))
    out = adduser.run({"email": email, "password": PW, "name": name, "role": ["site-admin"]}, ctx)
    assert out.ok, out.errors


def _client(email: str | None) -> TestClient:
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
        rec = next(r for r in load_user_records(live.accounts_path()) if r["email"] == email)
        client.cookies.set(SESSION_COOKIE, issue_session_token(User(**rec), issuer="http://testserver"))
    return client


def _agents(email: str = "ana@example.org") -> dict[str, dict]:
    r = _client(email).get("/api/v1/actors", params={"kind": "agent"})
    assert r.status_code == 200, r.text
    return {a["handle"]: a for a in r.json()["actors"]}


def _chain() -> list[dict]:
    from axiom.infra.paths import get_user_state_dir

    path = audit_trail.audit_path(get_user_state_dir())
    return audit_trail.read_chain(path) if path.exists() else []


# ---------------------------------------------------------------------------
# TEAM-AG-1: a fresh site
# ---------------------------------------------------------------------------


def test_team_ag_1_the_first_account_answers_for_the_assistant_and_nothing_else_is_listed():
    _first_account()
    agents = _agents()
    assert list(agents) == ["@axi"]
    axi = agents["@axi"]
    assert axi["accountable"] == "@ana:site-a" and axi["accountable_name"] == "Ana"
    assert "finding" not in axi and "issue" not in axi
    r = _client("ana@example.org").get("/api/v1/actors")
    assert r.json()["findings"] == []
    entry = activation.load()["agents"]["axi"]
    assert entry["active"] and entry["account"] == "ana@example.org"
    changes = [rec["change"] for rec in _chain() if rec.get("change")]
    assert {"kind": "agent", "subject": "@axi", "old": None,
            "new": {"active": True, "accountable": "@ana:site-a"}} in changes


def test_team_ag_1_the_catalog_offers_every_dormant_type_with_its_role():
    _first_account()
    r = _client("ana@example.org").get("/api/v1/actors/agent-types")
    assert r.status_code == 200, r.text
    types = {t["handle"]: t for t in r.json()["types"]}
    assert types["@axi"]["active"] is True
    assert types["@clerk"] == {"handle": "@clerk", "name": "CLERK", "role": "steward",
                               "does": "clerk work", "asks": ["ask clerk"], "origin": "built-in",
                               "active": False}


# ---------------------------------------------------------------------------
# TEAM-AG-2: adding an agent
# ---------------------------------------------------------------------------


def test_team_ag_2_the_person_who_adds_an_agent_answers_for_it():
    _first_account()
    _add_account("bo@example.org", "Bo", ["site-admin"])
    r = _client("bo@example.org").post("/api/v1/actors/agents/activate", json={"agent": "@clerk"})
    assert r.status_code == 200, r.text
    clerk = _agents()["@clerk"]
    assert clerk["accountable"] == "@bo:site-a" and clerk["accountable_name"] == "Bo"
    assert any(rec.get("skill") == "agents.activate" and rec["change"]["subject"] == "@clerk"
               for rec in _chain())


def test_team_ag_2_an_explicit_accountable_person_is_never_overwritten(monkeypatch):
    monkeypatch.setenv("AXIOM_AGENT_ACCOUNTABLE", "keep=@cy:site-a")
    _first_account()
    agents = _agents()
    assert sorted(agents) == ["@axi", "@keep"]      # configured by name: already set up here
    assert _client("ana@example.org").post("/api/v1/actors/agents/activate",
                                           json={"agent": "@rivet"}).status_code == 200
    agents = _agents()
    assert agents["@keep"]["accountable"] == "@cy:site-a"     # site configuration kept
    assert agents["@rivet"]["accountable"] == "@ben:site-a"   # the declaration kept
    refused = _client("ana@example.org").post("/api/v1/actors/agents/assign",
                                              json={"agent": "@keep", "person": "ana@example.org"})
    assert refused.status_code == 400 and "site configuration" in refused.json()["detail"]


def test_team_ag_2_a_member_cannot_add_or_see_the_catalog():
    _first_account()
    _add_account("ro@example.org", "Ro", ["reader"])
    member = _client("ro@example.org")
    assert member.get("/api/v1/actors/agent-types").status_code == 403
    assert member.post("/api/v1/actors/agents/activate", json={"agent": "@clerk"}).status_code == 403
    assert "@clerk" not in _agents("ro@example.org")


# ---------------------------------------------------------------------------
# TEAM-AG-3: site start, idempotent and audited
# ---------------------------------------------------------------------------


def test_team_ag_3_site_start_is_idempotent():
    _first_account()
    path = activation.record_path()
    before, chain = path.read_bytes(), len(_chain())
    assert activation.ensure_site_record(roster=ROSTER) == []
    assert activation.ensure_site_record(roster=ROSTER) == []
    assert path.read_bytes() == before and len(_chain()) == chain


def test_team_ag_3_an_existing_site_gives_its_active_agents_the_oldest_admin(monkeypatch):
    # A site from before this release: accounts, no record, an agent it configured by name.
    _add_account("ro@example.org", "Ro", ["reader"])
    _add_account("ana@example.org", "Ana", ["site-admin"])
    _add_account("bo@example.org", "Bo", ["site-admin"])
    monkeypatch.setenv("AXIOM_AGENT_ACCOUNTABLE", "keep=@cy:site-a")
    lines = activation.ensure_site_record(roster=ROSTER)
    assert any("oldest site administrator" in line for line in lines)
    agents = _agents()
    assert sorted(agents) == ["@axi", "@keep"]
    assert agents["@axi"]["accountable"] == "@ana:site-a"     # oldest admin, not the reader
    assert agents["@keep"]["accountable"] == "@cy:site-a"
    audited = {rec["change"]["subject"] for rec in _chain() if rec.get("skill") == "agents.site_start"}
    assert audited == {"@axi", "@keep"}
    assert activation.ensure_site_record(roster=ROSTER) == []


def test_team_ag_3_site_start_waits_for_an_administrator():
    _add_account("ro@example.org", "Ro", ["reader"])
    assert activation.ensure_site_record(roster=ROSTER) == []
    assert activation.load() is None


# ---------------------------------------------------------------------------
# TEAM-AG-4: lost access is an issue with its fix
# ---------------------------------------------------------------------------


def _suspend(email: str) -> None:
    from axiom.extensions.builtins.webgate.skills import suspend
    from axiom.infra.skills import SkillContext, SkillRegistry

    ctx = SkillContext(registry=SkillRegistry(), state_dir=live.accounts_path().parent,
                       logger=logging.getLogger("t"))
    assert suspend.suspend({"email": email, "reason": "left", "by": "ana@example.org"}, ctx).ok


def test_team_ag_4_a_suspended_accountable_person_is_an_issue_with_assign_as_its_fix():
    _first_account()
    _add_account("bo@example.org", "Bo", ["site-admin"])
    _client("bo@example.org").post("/api/v1/actors/agents/activate", json={"agent": "@clerk"})
    _suspend("bo@example.org")
    clerk = _agents()["@clerk"]
    assert clerk["issue"]["code"] == "accountable-suspended"
    assert clerk["issue"]["action"] == {"id": "assign", "label": "Assign someone"}
    assert "suspended" in clerk["issue"]["summary"]
    fixed = _client("ana@example.org").post("/api/v1/actors/agents/assign",
                                            json={"agent": "@clerk", "person": "ana@example.org"})
    assert fixed.status_code == 200, fixed.text
    clerk = _agents()["@clerk"]
    assert clerk["accountable"] == "@ana:site-a" and "issue" not in clerk


def test_team_ag_4_a_removed_accountable_person_is_an_issue():
    _first_account()
    _add_account("bo@example.org", "Bo", ["site-admin"])
    _client("bo@example.org").post("/api/v1/actors/agents/activate", json={"agent": "@clerk"})
    path = live.accounts_path()
    kept = [r for r in json.loads(path.read_text()) if r["email"] != "bo@example.org"]
    path.write_text(json.dumps(kept))
    assert _agents()["@clerk"]["issue"]["code"] == "accountable-removed"


def test_team_ag_4_assign_refuses_someone_who_cannot_sign_in():
    _first_account()
    _add_account("bo@example.org", "Bo", ["site-admin"])
    _suspend("bo@example.org")
    r = _client("ana@example.org").post("/api/v1/actors/agents/assign",
                                        json={"agent": "@axi", "person": "bo@example.org"})
    assert r.status_code == 400 and "no live account" in r.json()["detail"]


# ---------------------------------------------------------------------------
# TEAM-AG-5: deactivate, and dormant agents take no requests
# ---------------------------------------------------------------------------


def test_team_ag_5_deactivating_returns_an_agent_to_dormant_and_keeps_its_record():
    _first_account()
    admin = _client("ana@example.org")
    admin.post("/api/v1/actors/agents/activate", json={"agent": "@clerk"})
    r = admin.post("/api/v1/actors/agents/deactivate", json={"agent": "@clerk"})
    assert r.status_code == 200, r.text
    assert "@clerk" not in _agents()
    entry = activation.load()["agents"]["clerk"]
    assert entry["active"] is False and entry["accountable"] == "@ana:site-a"
    assert entry["deactivated_by"] == "ana@example.org"
    again = admin.post("/api/v1/actors/agents/deactivate", json={"agent": "@clerk"})
    assert again.status_code == 400


def test_team_ag_5_a_dormant_agent_takes_no_requests_and_an_active_one_does(monkeypatch):
    import types

    import axiom.extensions.builtins.connect.agent_router as router_mod
    from axiom.agents.delegation import DelegationError, delegate

    class Runner:
        def run(self, request, *, persona, namespace, requester):
            return f"done: {request}"

    spec = types.SimpleNamespace(namespace="program", namespaces=("program",), display="CLERK")
    monkeypatch.setattr(router_mod, "resolve_agent",
                        lambda key, roster, registry=None: types.SimpleNamespace(persona="p", spec=spec))
    _first_account()
    with pytest.raises(DelegationError, match="not on Team"):
        delegate("clerk", "status please", runner=Runner(), agents={a.handle: a for a in ROSTER})
    activation.activate("clerk", by="ana@example.org", roster=ROSTER)
    assert delegate("clerk", "status please", runner=Runner(),
                    agents={a.handle: a for a in ROSTER}).answer == "done: status please"


def test_team_ag_5_with_no_site_record_every_agent_still_takes_requests(monkeypatch):
    monkeypatch.delenv("AXIOM_GATE_USERS_FILE")
    assert activation.dormant_refusal("clerk") is None


# ---------------------------------------------------------------------------
# TEAM-AG-6: the roster stays whole
# ---------------------------------------------------------------------------


def test_team_ag_6_a_dormant_agent_is_still_never_taken_for_a_person():
    from axiom.extensions.builtins.features.approvals import person_refusal

    _first_account()
    assert activation.is_active("clerk") is False
    assert "platform agent" in (person_refusal("@clerk", "@ana") or "")
