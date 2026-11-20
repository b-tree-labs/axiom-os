# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Acceptance tests for unit L1: software proposes drafts, and a person sees them in a tray.

Ticket: docs/working/logbook-units/L1.md in the product repo that holds the plan, built against
`docs/working/design-note-l1-proposals.md`. Rows A1 to A7 are Axiom's; A8 and A9 (the tray and
resume) are appkit's, in `frontend/src/logbookAcceptance/l1.test.tsx`.
"""

from __future__ import annotations

import ast
import inspect
import logging

import pytest

pytest.importorskip("fastapi")

from axiom.extensions.builtins.attest import proposals, service  # noqa: E402
from axiom.extensions.builtins.attest.service import AttestRefused, Signatory  # noqa: E402
from axiom.extensions.builtins.attest.skills import SPECS, bind_default  # noqa: E402
from axiom.extensions.builtins.attest.tests.test_api import OP1, OP2, as_  # noqa: E402
from axiom.extensions.builtins.fleet import api as fleet_api  # noqa: E402
from axiom.infra.principal import PrincipalContext  # noqa: E402
from axiom.infra.skills import SkillContext  # noqa: E402
from axiom.webauth.api_keys import ApiKeyIdentity  # noqa: E402

SITE = "site-a"
ME = "@op1:site-a"
ROUND = {"site": SITE, "logbook": "demo_log", "type": "ROUND_CHECK", "meaning": "performed",
         "title": "Round check", "fields": {"walkdown": True}, "for_principal": ME}  # fmt: skip
TOKEN = "axk_k1_secret"


@pytest.fixture(autouse=True)
def known_callers(monkeypatch):
    """One agent on the roster and one installed extension, so the tests name both."""
    monkeypatch.setattr(proposals, "_agent_names", lambda: {"clerk"})
    monkeypatch.setattr(proposals, "_installed_extensions", lambda: {"example_ext"})


@pytest.fixture
def roles(client, tmp_path):
    """The state dir the HTTP client's routes read roles from, where @op1:site-a is an operator.
    Skills and routes share it, so a draft proposed one way is in the tray read the other."""
    return tmp_path


def ctx(state_dir, *, actor=None, surface="mcp", handle=ME):
    return SkillContext(
        registry=bind_default(), state_dir=state_dir, logger=logging.getLogger("test.l1"),
        principal=PrincipalContext(handle=handle, posture="sso", assured=True),
        actor=actor, surface=surface,
    )  # fmt: skip


def run(name, params, c):
    return c.registry.invoke(name, params, c)


def propose(roles, **over):
    r = run("attest.propose", {**ROUND, **over}, ctx(roles, actor="@clerk:site-a"))
    assert r.ok, r.errors
    return r.value


@pytest.fixture
def bearer(monkeypatch):
    """A key store that knows one token; each test sets what it resolves to."""
    who = {"identity": None}

    class Store:
        def resolve(self, token):
            return who["identity"] if token == TOKEN else None

    fleet_api.set_key_store_factory(lambda: Store())
    yield who
    fleet_api.reset_key_store_factory()


def key(principal="@clerk:site-a", scopes=("attest.propose",), site=SITE):
    return ApiKeyIdentity(key_id="k1", principal=principal, scopes=tuple(scopes), site=site)


def post_proposal(client, body):
    client.cookies.clear()
    return client.post(
        "/api/v1/attest/proposals", json=body, headers={"Authorization": f"Bearer {TOKEN}"}
    )


HTTP_ROUND = {"logbook": "demo_log", "entry_type": "ROUND_CHECK", "meaning": "performed",
              "title": "Round check", "fields": {"walkdown": True}, "for_principal": ME}  # fmt: skip


# -- acceptance rows ----------------------------------------------------------------------


@pytest.mark.acceptance("LB-L1-A1")
def test_l1_a1_a_proposal_reaches_the_recipients_tray_and_is_announced(attest_db, roles):
    from axiom.infra.bus import get_default_eventbus

    seen = []
    sub = get_default_eventbus().subscribe(
        "attest.demo_log.draft_created", lambda subject, payload: seen.append(payload), source="t"
    )
    try:
        d = propose(roles)
    finally:
        get_default_eventbus().unsubscribe(sub)
    assert d["origin"] == "agent:clerk" and d["for_principal"] == ME and d["status"] == "open"
    tray = run("attest.drafts", {}, ctx(roles, actor=None, surface="cli"))
    assert tray.ok, tray.errors
    assert [(t["draft_id"], t["origin"]) for t in tray.value["drafts"]] == [
        (d["draft_id"], "agent:clerk")
    ]
    assert seen == [{"schema_version": 1, "draft_id": d["draft_id"], "site_id": SITE,
                     "logbook": "demo_log", "entry_type": "ROUND_CHECK", "origin": "agent:clerk",
                     "for_principal": ME}]  # fmt: skip


@pytest.mark.acceptance("LB-L1-A2")
def test_l1_a2_nothing_signs_for_software(attest_db, roles, node_signer):
    by_name = {s.name: s for s in SPECS}
    for name in ("attest.propose", "attest.drafts"):
        module = inspect.getmodule(by_name[name].fn)
        tree = ast.parse(inspect.getsource(module))
        called = {
            n.func.attr for n in ast.walk(tree)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
        }  # fmt: skip
        assert not called & {"sign", "respond"}, name
    assert by_name["attest.sign"].surfaces == ("cli",)
    d = propose(roles)
    service.fill(d["draft_id"], {"reading": "ok"},
                 by=Signatory(principal=PrincipalContext(handle=ME), roles=()))  # fmt: skip
    p = service.present(d["draft_id"], modality="cli")
    agent = PrincipalContext(handle="@clerk:site-a", posture="sso", assured=True)
    with pytest.raises(AttestRefused, match="is for @op1:site-a"):
        service.respond(p.presentation_id, principal=agent, answer="sign", via="cli")
    with pytest.raises(AttestRefused, match="is for @op1:site-a"):
        service.sign(p.presentation_id, signer=node_signer,
                     signatory=Signatory(principal=agent, roles=("operator",)))  # fmt: skip


@pytest.mark.acceptance("LB-L1-A3")
def test_l1_a3_software_never_supplies_an_observed_value(attest_db, roles, client, bearer):
    r = run("attest.propose", {**ROUND, "fields": {"reading": "ok"}},
            ctx(roles, actor="@clerk:site-a"))  # fmt: skip
    assert not r.ok and "observe" in r.errors[0]
    bearer["identity"] = key()
    h = post_proposal(client, {**HTTP_ROUND, "fields": {"reading": "ok"}})
    assert h.status_code == 422 and "observe" in h.json()["detail"]
    assert service.list_drafts(for_principal=ME) == []


@pytest.mark.acceptance("LB-L1-A4")
def test_l1_a4_the_origin_comes_from_the_caller_never_the_request(attest_db, roles, client, bearer):
    for bad in ("human", "", "agent", "agent:", "speech", "Agent:X", "rule"):
        with pytest.raises(AttestRefused):
            proposals.validate_origin(bad)
    for good in ("agent:clerk", "integration:example_ext", "rule:due-soon"):
        assert proposals.validate_origin(good) == good
    named = run("attest.propose", {**ROUND, "origin": "human"}, ctx(roles, actor="@clerk:site-a"))
    assert not named.ok and "origin" in named.errors[0]
    bare = run("attest.propose", ROUND, ctx(roles, actor=None, surface="cli"))
    assert not bare.ok and "--integration" in bare.errors[0]
    cli = run("attest.propose", {**ROUND, "integration": "example_ext"},
              ctx(roles, actor=None, surface="cli"))  # fmt: skip
    assert cli.ok and cli.value["origin"] == "integration:example_ext"
    not_installed = run("attest.propose", {**ROUND, "integration": "nope"},
                        ctx(roles, actor=None, surface="cli"))  # fmt: skip
    assert not not_installed.ok
    over_mcp = run("attest.propose", {**ROUND, "integration": "example_ext"},
                   ctx(roles, actor=None, surface="mcp"))  # fmt: skip
    assert not over_mcp.ok
    mcp = run("attest.propose", ROUND, ctx(roles, actor=None, surface="mcp"))
    assert mcp.ok and mcp.value["origin"] == "agent:mcp"
    bearer["identity"] = key()
    assert post_proposal(client, {**HTTP_ROUND, "origin": "human"}).status_code == 400
    ok = post_proposal(client, HTTP_ROUND)
    assert ok.status_code == 201, ok.text
    assert ok.json()["origin"] == "agent:clerk"
    bearer["identity"] = key(principal="@example_ext:site-a")
    assert post_proposal(client, HTTP_ROUND).json()["origin"] == "integration:example_ext"
    bearer["identity"] = key(principal="@op2:site-a")
    assert post_proposal(client, HTTP_ROUND).status_code == 403
    bearer["identity"] = key(scopes=("receipts.read",))
    assert post_proposal(client, HTTP_ROUND).status_code == 403
    bearer["identity"] = key(site=None)
    assert post_proposal(client, HTTP_ROUND).status_code == 403
    as_(client, OP1)  # a person's browser session cannot propose, key or no key
    session = client.post("/api/v1/attest/proposals", json=HTTP_ROUND,
                          headers={"Authorization": f"Bearer {TOKEN}"})  # fmt: skip
    assert session.status_code == 403
    no_key = client.post("/api/v1/attest/proposals", json=HTTP_ROUND)
    assert no_key.status_code in (401, 403)
    assert {d["origin"] for d in service.list_drafts(for_principal=ME)} == {
        "agent:clerk",
        "integration:example_ext",
        "agent:mcp",
    }


@pytest.mark.acceptance("LB-L1-A5")
def test_l1_a5_the_recipient_is_a_person_who_may_sign_the_type(attest_db, roles):
    proposals.check_recipient(SITE, "demo_log", "ROUND_CHECK", ME, roles)
    for who, why in (("@clerk:site-a", "agent"), ("op1", "handle"), ("@op1", "context"),
                     ("@op2:site-a", "role")):  # fmt: skip
        with pytest.raises(AttestRefused, match=why):
            proposals.check_recipient(SITE, "demo_log", "ROUND_CHECK", who, roles)
        r = run("attest.propose", {**ROUND, "for_principal": who},
                ctx(roles, actor="@clerk:site-a"))  # fmt: skip
        assert not r.ok, who


@pytest.mark.acceptance("LB-L1-A6")
def test_l1_a6_the_tray_is_private(attest_db, roles, client):
    d = propose(roles)
    other = run("attest.drafts", {}, ctx(roles, actor=None, surface="cli", handle="@op2:site-a"))
    assert other.ok and other.value["drafts"] == []
    as_(client, OP2)
    assert client.get("/api/v1/attest/drafts").json()["drafts"] == []
    assert client.get(f"/api/v1/attest/drafts/{d['draft_id']}").status_code == 404
    assert client.delete(f"/api/v1/attest/drafts/{d['draft_id']}").status_code == 404
    with pytest.raises(AttestRefused):
        service.discard(d["draft_id"], by="@op2:site-a")
    as_(client, OP1)
    mine = client.get("/api/v1/attest/drafts?origin=agent&status=open").json()["drafts"]
    assert [m["draft_id"] for m in mine] == [d["draft_id"]] and mine[0]["created_at"]
    assert client.delete(f"/api/v1/attest/drafts/{d['draft_id']}").status_code == 200
    assert client.get("/api/v1/attest/drafts").json()["drafts"] == []
    assert service.draft(d["draft_id"])["status"] == "discarded"


@pytest.mark.acceptance("LB-L1-A7")
def test_l1_a7_at_most_25_open_proposals_per_origin_and_recipient(attest_db, roles):
    assert proposals.MAX_OPEN_PER_ORIGIN == 25
    for _ in range(25):
        propose(roles)
    r = run("attest.propose", ROUND, ctx(roles, actor="@clerk:site-a"))
    assert not r.ok and "25 open proposals" in r.errors[0]
    another = run("attest.propose", ROUND, ctx(roles, actor="@keep:site-a"))
    assert another.ok, another.errors
    first = service.list_drafts(for_principal=ME, origin_prefix="agent:clerk")[0]
    service.discard(first["draft_id"], by=ME)
    assert propose(roles)["origin"] == "agent:clerk"
