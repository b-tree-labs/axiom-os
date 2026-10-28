# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""ADR-203 on a real Postgres: a kit promoted as data, and what it can never do.

The node here is a kit medallion with another site's rows added to silver and a
pre-existing reader role (standing in for the gold door's connection). The
contributor's kit is promoted as a bundle, checked, approved and applied, and
then its verb is run. What must hold:

- a bundle carrying Python is refused, and a credential bound to one site
  cannot promote a kit for another;
- a gold object that tries to read every site sees only its own, because the
  role it runs as is admitted to its site's rows and no others;
- the reader that existed before the first kit still sees every site;
- the structural proof fails when row-level security is switched off
  (negative control: a proof that cannot fail proves nothing).

Needs Docker (or AXIOM_KIT_DSN); marked ``integration``.
"""

from __future__ import annotations

import os
import shutil

import pytest

from axiom.extensions.builtins.data_platform import kit
from axiom.extensions.builtins.data_platform.kit import declarations as decl
from axiom.extensions.builtins.data_platform.kit import host
from axiom.extensions.builtins.data_platform.kit.bundle import pack
from axiom.extensions.builtins.data_platform.kit.medallion import connect, dsn
from axiom.extensions.builtins.data_platform.kit.project import KitError

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not (shutil.which("docker") or os.environ.get("AXIOM_KIT_DSN")),
        reason="needs Docker or AXIOM_KIT_DSN",
    ),
]

SITE, OTHER = "example-loop", "other-loop"
LEAK_SQL = "SELECT site, count(*) AS n FROM silver.signals GROUP BY site\n"
LEAK_VERB = """name = "sites_i_can_see"
description = "Every site whose rows this kit can read, with a row count."
object = "every_site"
sql = "SELECT site, n FROM {object} ORDER BY site"
"""


@pytest.fixture(scope="module")
def node(tmp_path_factory):
    """A medallion holding SITE's rows and OTHER's rows, plus a reader role."""
    repo = tmp_path_factory.mktemp("node-site")
    kit.init(repo, SITE)
    p = kit.load(repo)
    kit.up(p)
    kit.try_kit(p)  # conforms the examples: SITE's rows land in silver
    d = dsn(p)
    with connect(d) as conn:
        conn.execute(
            """INSERT INTO silver.signals (site, feed, channel, ts, value, unit, quality,
                   source_class, schema_ref, row_hash, role, derivation)
               SELECT %s, feed, channel, ts, value, unit, quality, source_class, schema_ref,
                      'other:' || row_hash, role, derivation
               FROM silver.signals WHERE site = %s ON CONFLICT DO NOTHING""",
            (OTHER, SITE),
        )
        conn.execute("DROP POLICY IF EXISTS kit_example_loop_rows ON silver.signals")
        conn.execute("ALTER TABLE silver.signals DISABLE ROW LEVEL SECURITY")  # as a fresh node
        conn.execute(
            "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='door_reader') "
            "THEN CREATE ROLE door_reader NOLOGIN; END IF; END $$"
        )
        conn.execute("GRANT USAGE ON SCHEMA silver TO door_reader")
        conn.execute("GRANT SELECT ON silver.signals TO door_reader")
    # The registry alone, as an operator's kit-host-setup leaves it before any reader
    # policy: the approve below still has to give door_reader its policy.
    host.ensure_registry(d)
    yield p, d
    kit.down(p)


@pytest.fixture
def contribution(tmp_path):
    """The contributor's kit: declarations only, plus a gold object that tries to see every site."""
    repo = tmp_path / "contrib"
    kit.init(repo, SITE)
    p = kit.load(repo)
    shutil.rmtree(p.root / "conform")
    (p.root / "gold" / "every_site.sql").write_text(LEAK_SQL)
    (p.root / "gold" / "every_site.toml").write_text(
        'title = "Sites this kit can see"\n'
        'description = "Each site whose silver rows this kit can read, with a row count."\n'
    )
    (p.root / "verbs" / "sites_i_can_see.toml").write_text(LEAK_VERB)
    return kit.load(repo)


def test_a_bundle_with_code_is_refused(node, tmp_path):
    _, d = node
    repo = tmp_path / "withcode"
    kit.init(repo, SITE)
    b = pack(kit.load(repo), include_code=True)  # promote never does this; the node must still refuse it
    assert b.has_code
    r = host.submit(d, b.data, caller_site=SITE, principal="@contrib:example-loop", home=False)
    assert r["state"] == "rejected" and any("conform code" in p for p in r["problems"])


def test_a_site_promotes_only_its_own_kit(node, contribution):
    _, d = node
    with pytest.raises(KitError, match="may only promote a kit for that site"):
        host.submit(d, pack(contribution).data, caller_site=OTHER, principal="@x:other-loop", home=False)


def test_promoted_kit_is_served_and_isolated(node, contribution):
    _, d = node
    r = host.submit(d, pack(contribution).data, caller_site=SITE, principal="@contrib:example-loop", home=False)
    assert r["state"] == "checked", r["problems"]
    applied = host.approve(d, r["id"], approver="@operator:home")
    assert applied["state"] == "applied"
    assert "sites_i_can_see" in applied["verbs"]
    assert "door_reader" in applied["reader_policies_added"]

    names = {v["name"] for v in host.list_verbs(d, site=SITE)}
    assert {"sites_i_can_see", "temperature_rise_by_day"} <= names

    seen = host.run_registered_verb(d, site=SITE, name="sites_i_can_see")
    assert [row["site"] for row in seen] == [SITE], "the kit's role sees only its own site"

    rises = host.run_registered_verb(d, site=SITE, name="temperature_rise_by_day")
    assert rises and all(row["unit"] == "degC" for row in rises)

    with connect(d, role="door_reader") as conn:
        sites = {s for (s,) in conn.execute("SELECT DISTINCT site FROM silver.signals")}
    assert sites == {SITE, OTHER}, "a reader from before the first kit still sees every site"

    with pytest.raises(KitError, match="no parameter"):
        host.run_registered_verb(d, site=SITE, name="sites_i_can_see", params={"site": OTHER})


def test_the_structural_proof_fails_without_row_security(node, contribution):
    """Negative control."""
    p, d = node
    import psycopg

    with connect(d) as conn:
        conn.execute("ALTER TABLE silver.signals DISABLE ROW LEVEL SECURITY")
    try:
        with psycopg.connect(d) as conn, conn.cursor() as cur:
            problems = host.structural_proof(cur, contribution, decl.load(contribution))
            conn.rollback()
        assert any("row-level security is not enabled" in x for x in problems)
    finally:
        with connect(d) as conn:
            conn.execute("ALTER TABLE silver.signals ENABLE ROW LEVEL SECURITY")


# -- through the served door (/data/query), as a bound laptop reaches it ---------------


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


def test_the_door_serves_promotion_and_holds_each_key_to_its_site(node, contribution, monkeypatch):
    import base64

    _, d = node
    monkeypatch.setenv("DP1_RAG_DSN", d)
    monkeypatch.setenv("AXIOM_SITE", "home-site")
    bundle = base64.b64encode(pack(contribution).data).decode()

    mine = _door(SITE, "@contrib:example-loop")
    theirs = _door(OTHER, "@someone:other-loop")

    refused = theirs.post("/data/submit", json={"verb": "data.kit_submit", "params": {"bundle": bundle}}).json()
    assert refused["ok"] is False and "may only promote a kit for that site" in refused["errors"][0]

    sent = mine.post("/data/submit", json={
        "verb": "data.kit_submit",
        "params": {"bundle": bundle, "_principal": "@forged:home-site", "dsn": "postgresql://evil/"},
    }).json()
    assert sent["ok"] is True and sent["value"]["state"] == "checked", sent
    rows = host.review(d, site=SITE, state="checked")
    assert rows[0]["submitted_by"] == "@contrib:example-loop", "the principal comes from the gate, not the body"
    host.approve(d, sent["value"]["id"], approver="@operator:home-site")

    listed = mine.post("/data/query", json={"verb": "data.kit_verbs", "params": {}}).json()
    assert {v["site"] for v in listed["value"]["verbs"]} == {SITE}
    seen = mine.post("/data/query", json={"verb": "data.kit_verb", "params": {"name": "sites_i_can_see"}}).json()
    assert [r["site"] for r in seen["value"]["rows"]] == [SITE]

    charts = mine.post("/data/query", json={"verb": "data.kit_charts", "params": {}}).json()
    assert charts["ok"] and {c["site"] for c in charts["value"]["charts"]} == {SITE} and charts["value"]["charts"]
    assert theirs.post("/data/query", json={"verb": "data.kit_charts", "params": {}}).json()["value"]["charts"] == []
    other_list = theirs.post("/data/query", json={"verb": "data.kit_verbs", "params": {}}).json()
    assert other_list["value"]["verbs"] == [], "another site's key lists none of this site's verbs"
    blocked = theirs.post("/data/query", json={
        "verb": "data.kit_verb", "params": {"name": "sites_i_can_see", "site": SITE}}).json()
    assert blocked["ok"] is False and "reads site 'other-loop' only" in blocked["errors"][0]


def test_a_security_definer_function_the_role_can_run_stops_the_apply(node, contribution):
    """The other leak route the kit PRD names: a function running with its owner's rights."""
    _, d = node
    with connect(d) as conn:
        conn.execute(
            "CREATE OR REPLACE FUNCTION gold.kit_leak_probe() RETURNS bigint "
            "LANGUAGE sql SECURITY DEFINER AS 'SELECT count(*) FROM silver.signals'"
        )
    try:
        r = host.submit(d, pack(contribution).data, caller_site=SITE, principal="@contrib:example-loop", home=False)
        with pytest.raises(KitError, match="SECURITY DEFINER"):
            host.approve(d, r["id"], approver="@operator:home")
        assert host.review(d, site=SITE, state="rejected"), "the bundle is left rejected with the reason"
    finally:
        with connect(d) as conn:
            conn.execute("DROP FUNCTION IF EXISTS gold.kit_leak_probe()")


# -- kit-pull: start a kit from a frame that already exists ------------------------------


def _ctx(tmp_path):
    import logging

    from axiom.infra.skills import SkillContext, SkillRegistry

    return SkillContext(registry=SkillRegistry(), state_dir=tmp_path, logger=logging.getLogger("t"))


def test_kit_pull_keeps_a_sites_series_as_a_snapshot(node, contribution, monkeypatch, tmp_path):
    from axiom.extensions.builtins.data_platform.skills import kit_promotion

    _, d = node
    monkeypatch.setenv("DP1_RAG_DSN", d)  # on the node: reads its own medallion
    import psycopg

    from axiom.extensions.builtins.data_platform.conformance import catalogue

    with psycopg.connect(d) as conn, conn.cursor() as cur:  # a node keeps one; kit-try does not
        catalogue.rebuild(cur, SITE)
    r = kit_promotion.kit_pull({"dir": str(contribution.root), "site": SITE, "start": "2026-08-01",
                                "end": "2026-11-01", "bucket": "1 hour", "id": "pulled"}, _ctx(tmp_path))
    assert r.ok, r.errors
    kept = r.value["rows"]
    assert (len(kept) if isinstance(kept, list) else kept) > 0 and r.value["from"] == "medallion"
    snap = next((contribution.root / "snapshots").glob("pulled*"))
    assert snap.exists()


def test_kit_pull_through_the_door_is_held_to_the_keys_site(node, contribution, monkeypatch, tmp_path):
    """A bound laptop whose key belongs to another site pulls nothing of this site's."""
    from axiom.extensions.builtins.data_platform import read_target
    from axiom.extensions.builtins.data_platform.skills import kit_promotion

    _, d = node
    monkeypatch.setenv("DP1_RAG_DSN", d)
    door = _door(OTHER, "@someone:other-loop")
    real = read_target.resolve_read_target
    calls = {"n": 0}

    def laptop_then_node():
        # In one process the laptop and the node share this module: only the first call
        # (the laptop's kit-pull) is bound; the node's own read inside the door is local.
        calls["n"] += 1
        if calls["n"] == 1:
            return read_target.ReadTarget("binding", site=OTHER, face_url="http://node.example.org")
        return real()

    monkeypatch.setattr(read_target, "resolve_read_target", laptop_then_node)
    monkeypatch.setattr(read_target, "_post",
                        lambda target, *, verb, params: door.post("/data/query", json={"verb": verb, "params": params}).json())
    r = kit_promotion.kit_pull({"dir": str(contribution.root), "site": SITE, "start": "2026-08-01",
                                "end": "2026-11-01", "bucket": "1 hour"}, _ctx(tmp_path))
    assert not r.ok and "reads only 'other-loop', not 'example-loop'" in r.errors[0], r


def test_each_credential_through_the_door_is_metered_as_itself(node, monkeypatch):
    """Two keys reading at once are two callers, not one shared anonymous slot."""
    from axiom.extensions.builtins.data_platform.skills import gold

    _, d = node
    monkeypatch.setenv("DP1_RAG_DSN", d)
    a, b = _door(SITE, "@a:example-loop"), _door(SITE, "@b:example-loop")
    q = {"verb": "data.tables", "params": {}}
    assert a.post("/data/query", json=q).json()["ok"] and b.post("/data/query", json=q).json()["ok"]
    seen = set(gold._guard().metering())
    assert {"@a:example-loop", "@b:example-loop"} <= seen, seen
