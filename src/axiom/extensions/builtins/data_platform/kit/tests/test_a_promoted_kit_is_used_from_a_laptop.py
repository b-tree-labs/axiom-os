# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Using a promoted kit from a bound laptop, on a real Postgres with two sites' kits.

A live proof got through promote, approve, re-promote, withdraw and retire, then failed
at using the promoted kit from the laptop. What must hold:

- A credential bound to a site lists, sees and runs that site's kit objects only, even
  when it is the node's home site, and a ``site`` param naming another site is refused
  ("this credential reads site 'A' only"), not answered as "no promoted verb". The site
  comes from the verified credential; only an operator (no site on the credential) may
  name one.
- ``kit-verbs``, ``kit-charts``, ``kit-automations`` and ``kit-verb`` on a bound laptop go
  to the node, as ``kit-promote`` and ``kit-pull`` do; unbound, they read locally and say so.
- A verb that runs past the statement timeout is a refusal naming the bound, not HTTP 500.
- ``kit-retire`` without ``--apply`` or ``--force`` lists and changes nothing.

Each test starts from its own module's node (a fresh kit medallion), so nothing depends on
what another module left in a shared database. Needs Docker (or AXIOM_KIT_DSN).
"""

from __future__ import annotations

import logging
import os
import shutil

import pytest

from axiom.extensions.builtins.data_platform import kit
from axiom.extensions.builtins.data_platform.kit import host
from axiom.extensions.builtins.data_platform.kit.bundle import pack
from axiom.extensions.builtins.data_platform.kit.medallion import connect, dsn

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not (shutil.which("docker") or os.environ.get("AXIOM_KIT_DSN")),
        reason="needs Docker or AXIOM_KIT_DSN",
    ),
]

A, B = "use-site-a", "use-site-b"
OP = "@operator:node"
VERB = "temperature_rise_by_day"  # the template's verb: both sites promote one by this name


@pytest.fixture(scope="module")
def d(tmp_path_factory):
    """A node with an applied kit for each of two sites."""
    repo = tmp_path_factory.mktemp("use-node")
    kit.init(repo, "use-node")
    p = kit.load(repo)
    kit.up(p)
    kit.try_kit(p)
    out = dsn(p)
    host.setup(out)
    for site in (A, B):
        root = tmp_path_factory.mktemp(site)
        kit.init(root, site)
        r = host.submit(out, pack(kit.load(root)).data, caller_site=None, principal=f"@c:{site}", home=True)
        assert r["state"] == "checked", r["problems"]
        host.approve(out, r["id"], approver=OP)
    yield out
    kit.down(p)


@pytest.fixture
def node(d, monkeypatch):
    """The node's own process: its DSN, and A as its home site (the live case: the key's
    site is the site the node serves, which ``tenant_scope`` treats as unscoped)."""
    from axiom.extensions.builtins.data_platform import read_api

    monkeypatch.setenv("DP1_RAG_DSN", d)
    monkeypatch.setenv("AXIOM_SITE", A)
    monkeypatch.setattr(read_api, "holds_medallion", lambda: True)
    return d


def _door(site: str | None, principal: str):
    """The real data router behind a stand-in for the gate: it binds the key's site."""
    from fastapi import FastAPI, Request
    from fastapi.testclient import TestClient

    from axiom.extensions.builtins.data_platform.read_api import build_data_read_router
    from axiom.infra.site_authority import bind_caller_site, unbind_caller_site

    app = FastAPI()

    @app.middleware("http")
    async def gate(request: Request, call_next):
        request.state.principal = principal
        token = bind_caller_site(site)
        try:
            return await call_next(request)
        finally:
            unbind_caller_site(token)

    app.include_router(build_data_read_router())
    return TestClient(app)


def _ask(door, verb: str, params: dict | None = None) -> dict:
    r = door.post("/data/query", json={"verb": verb, "params": params or {}})
    assert r.status_code == 200, (r.status_code, r.text)
    return r.json()


def _ctx(tmp_path):
    from axiom.infra.skills import SkillContext, SkillRegistry

    return SkillContext(registry=SkillRegistry(), state_dir=tmp_path, logger=logging.getLogger("t"))


# -- 1. the site on the credential is the only site a key reads -------------------------------


def test_a_site_key_lists_and_runs_only_its_own_sites_kit_even_as_the_home_site(node):
    assert {v["name"] for v in host.list_verbs(node, site=B)} >= {VERB}, "B's verb exists"
    mine = _door(A, f"@x:{A}")

    verbs = _ask(mine, "data.kit_verbs")
    assert verbs["ok"] and {v["site"] for v in verbs["value"]["verbs"]} == {A}, verbs
    charts = _ask(mine, "data.kit_charts")
    assert charts["ok"] and charts["value"]["charts"] and {c["site"] for c in charts["value"]["charts"]} == {A}
    autos = _ask(mine, "data.kit_automations")
    assert autos["ok"] and {a["site"] for a in autos["value"]["automations"]} <= {A}

    ran = _ask(mine, "data.kit_verb", {"name": VERB})
    assert ran["ok"] and ran["value"]["site"] == A, "no site param needed: it is the credential's"

    for verb, params in (("data.kit_verbs", {"site": B}), ("data.kit_charts", {"site": B}),
                         ("data.kit_automations", {"site": B}), ("data.kit_verb", {"name": VERB, "site": B})):
        refused = _ask(mine, verb, params)
        assert refused["ok"] is False, (verb, refused)
        assert f"this credential reads site '{A}' only" in refused["errors"][0], (verb, refused)


def test_a_site_key_cannot_submit_or_withdraw_for_another_site_even_as_the_home_site(node, tmp_path):
    import base64

    root = tmp_path / "theirs"
    kit.init(root, B)
    bundle = base64.b64encode(pack(kit.load(root)).data).decode()
    mine = _door(A, f"@x:{A}")
    sent = mine.post("/data/submit", json={"verb": "data.kit_submit", "params": {"bundle": bundle}}).json()
    assert sent["ok"] is False and "may only promote a kit for that site" in sent["errors"][0], sent
    gone = mine.post("/data/submit", json={"verb": "data.kit_withdraw", "params": {"site": B}}).json()
    assert gone["ok"] is False and "only withdraw from that site" in gone["errors"][0], gone


def test_an_operator_with_no_site_on_the_credential_may_name_a_site(node, tmp_path):
    from axiom.extensions.builtins.data_platform.skills import kit_promotion

    listed = kit_promotion.kit_verbs({"site": B}, _ctx(tmp_path))
    assert listed.ok and {v["site"] for v in listed.value["verbs"]} == {B}
    ran = kit_promotion.kit_verb({"name": VERB, "site": B}, _ctx(tmp_path))
    assert ran.ok and ran.value["site"] == B


# -- 2. a bound laptop's kit reads go to the node -----------------------------------------------


@pytest.fixture
def laptop(node, monkeypatch):
    """One process plays both: the laptop's first resolve is bound; the node's own, inside
    the door, is local. Every request the laptop sends is recorded."""
    from axiom.extensions.builtins.data_platform import read_target

    door = _door(A, f"@x:{A}")
    real = read_target.resolve_read_target
    sent: list[str] = []
    calls = {"n": 0}

    def laptop_then_node():
        calls["n"] += 1
        if calls["n"] == 1:
            return read_target.ReadTarget("binding", site=A, face_url="http://node.example.org")
        return real()

    def post(target, *, verb, params):
        sent.append(verb)
        return door.post("/data/query", json={"verb": verb, "params": params}).json()

    monkeypatch.setattr(read_target, "resolve_read_target", laptop_then_node)
    monkeypatch.setattr(read_target, "_post", post)
    return sent, calls


@pytest.mark.parametrize("verb,params,key", [
    ("kit_verbs", {}, "verbs"),
    ("kit_charts", {}, "charts"),
    ("kit_automations", {}, "automations"),
])
def test_a_bound_laptops_kit_lists_come_from_the_node(laptop, tmp_path, verb, params, key):
    from axiom.extensions.builtins.data_platform.skills import kit_promotion

    sent, _ = laptop
    r = getattr(kit_promotion, verb)(params, _ctx(tmp_path))
    assert r.ok, r.errors
    assert sent == [f"data.{verb}"], "asked the node, not this machine's database"
    assert {row["site"] for row in r.value[key]} <= {A}, "held to the key's site there"
    assert any("current binding" in a for a in r.actions_taken), r.actions_taken


def test_a_bound_laptop_runs_a_kit_verb_on_the_node_and_is_held_to_its_site(laptop, tmp_path):
    from axiom.extensions.builtins.data_platform.skills import kit_promotion

    sent, calls = laptop
    ran = kit_promotion.kit_verb({"name": VERB}, _ctx(tmp_path))
    assert ran.ok and ran.value["site"] == A and sent == ["data.kit_verb"], (ran, sent)
    calls["n"] = 0
    refused = kit_promotion.kit_verb({"name": VERB, "site": B}, _ctx(tmp_path))
    assert not refused.ok and f"reads site '{A}' only" in refused.errors[0], refused


def test_an_unbound_install_reads_its_own_registry_and_says_it_is_local(node, monkeypatch, tmp_path):
    from axiom.extensions.builtins.data_platform import read_target
    from axiom.extensions.builtins.data_platform.skills import kit_promotion

    monkeypatch.setattr(read_target, "resolve_read_target",
                        lambda: read_target.ReadTarget("medallion", reason="no binding; the medallion on this install"))
    monkeypatch.setattr(read_target, "_post", lambda *a, **k: pytest.fail("an unbound install asks nobody"))
    r = kit_promotion.kit_verbs({}, _ctx(tmp_path))
    assert r.ok and {v["site"] for v in r.value["verbs"]} >= {A, B}
    assert any(a.startswith("local:") for a in r.actions_taken), r.actions_taken


# -- 3. a verb past the statement timeout is a refusal, not a 500 -------------------------------


def test_a_verb_past_the_statement_timeout_is_a_refusal_not_a_500(node, monkeypatch):
    with connect(node) as conn:
        (old,) = conn.execute(f"SELECT sql FROM {host.SCHEMA}.verbs WHERE site=%s AND name=%s", (A, VERB)).fetchone()
        conn.execute(f"UPDATE {host.SCHEMA}.verbs SET sql=%s WHERE site=%s AND name=%s",
                     ("SELECT pg_sleep(2)::text AS slept, (SELECT count(*) FROM {object}) AS n "
                      "WHERE %(since)s::date IS NOT NULL", A, VERB))
    monkeypatch.setattr(host, "VERB_TIMEOUT_MS", 100)
    try:
        r = _ask(_door(A, f"@x:{A}"), "data.kit_verb", {"name": VERB})
        assert r["ok"] is False and "did not finish within 0.1s; narrow its window" in r["errors"][0], r
    finally:
        with connect(node) as conn:
            conn.execute(f"UPDATE {host.SCHEMA}.verbs SET sql=%s WHERE site=%s AND name=%s", (old, A, VERB))


# -- 4. kit-retire lists first ------------------------------------------------------------------


def test_kit_retire_without_apply_lists_and_changes_nothing(node, tmp_path):
    from axiom.extensions.builtins.data_platform.skills import kit_promotion

    site = "use-site-r"
    root = tmp_path / site
    kit.init(root, site)
    r = host.submit(node, pack(kit.load(root)).data, caller_site=None, principal="@c:r", home=True)
    host.approve(node, r["id"], approver=OP)

    listed = kit_promotion.kit_retire({"site": site}, _ctx(tmp_path))
    assert listed.ok and "gold_use_site_r.daily_stats" in listed.value["would_drop"], listed
    assert listed.actions_taken[-1].endswith(f"(kit-retire {site} --apply)"), listed.actions_taken
    assert {v["name"] for v in host.list_verbs(node, site=site)} == {VERB}, "nothing was dropped"

    with connect(node) as conn:
        conn.execute("CREATE VIEW public.leans_on_use_r AS SELECT * FROM gold_use_site_r.daily_stats")
    try:
        listed = kit_promotion.kit_retire({"site": site, "principal": "@c:r"}, _ctx(tmp_path))
        assert listed.ok and "public.leans_on_use_r" in listed.value["dependents"], listed
        assert "--force to also drop the dependents outside the kit" in listed.actions_taken[-1]
        refused = kit_promotion.kit_retire({"site": site, "apply": True}, _ctx(tmp_path))
        assert not refused.ok and host.list_verbs(node, site=site), "--apply is still RESTRICT"
        done = kit_promotion.kit_retire({"site": site, "force": True}, _ctx(tmp_path))
        assert done.ok and host.list_verbs(node, site=site) == []
    finally:
        with connect(node) as conn:
            conn.execute("DROP VIEW IF EXISTS public.leans_on_use_r")
