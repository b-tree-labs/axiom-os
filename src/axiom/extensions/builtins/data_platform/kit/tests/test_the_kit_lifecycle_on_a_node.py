# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""A promoted kit's life after its first approve, on a real Postgres.

- ``kit-rollback`` re-applies an earlier bundle from the registry, with the same
  isolation proof, and the bundle it replaces becomes superseded.
- Ownership within a site: what a bundle creates is recorded with the principal
  that submitted it, and another contributor's bundle may not replace or drop it
  unless the operator overrides.
- ``kit-retire`` is RESTRICT by default: it lists what it would drop, refuses when
  something outside the kit depends on it, and ``--force`` cascades. It can retire
  one contributor's objects only. The registry keeps the audit record.
- ``kit-withdraw`` is a contributor's request, through the door as an invoke; it
  drops nothing, and the operator applies it with ``kit-retire --principal``.

Needs Docker (or AXIOM_KIT_DSN); marked ``integration``.
"""

from __future__ import annotations

import os
import shutil

import pytest

from axiom.extensions.builtins.data_platform import kit
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

A, B = "@a:contrib", "@b:contrib"
OP = "@operator:home"


@pytest.fixture(scope="module")
def d(tmp_path_factory):
    repo = tmp_path_factory.mktemp("life-node")
    kit.init(repo, "life-node")
    p = kit.load(repo)
    kit.up(p)
    kit.try_kit(p)
    out = dsn(p)
    host.setup(out)
    yield out
    kit.down(p)


def _kit(tmp_path, site: str, tag: str, *, views: dict[str, str] | None = None, template: bool = True):
    """A kit for ``site``: the template's objects, plus ``views`` (name -> SQL), each with a verb."""
    root = tmp_path / f"{site}-{tag}"
    kit.init(root, site)
    p = kit.load(root)
    if not template:
        for folder in ("gold", "verbs", "charts"):
            for f in p.folder(folder).iterdir():
                f.unlink()
    for name, sql in (views or {}).items():
        (p.folder("gold") / f"{name}.sql").write_text(sql)
        (p.folder("gold") / f"{name}.toml").write_text(f'description = "{name}, for a test"\n')
        (p.folder("verbs") / f"{name}_rows.toml").write_text(
            f'name = "{name}_rows"\ndescription = "Rows of {name}."\nobject = "{name}"\n'
            'sql = "SELECT * FROM {object} LIMIT 5"\n')
    return kit.load(root)


def _send(d, project, principal):
    r = host.submit(d, pack(project).data, caller_site=None, principal=principal, home=True)
    assert r["state"] == "checked", r["problems"]
    return r["id"]


def _verbs(d, site):
    return {v["name"] for v in host.list_verbs(d, site=site)}


ONE = "SELECT channel, count(*) AS n FROM silver.signals GROUP BY channel\n"


def test_rollback_reapplies_an_earlier_bundle_and_supersedes_the_current(d, tmp_path):
    site = "roll-site"
    first = _send(d, _kit(tmp_path, site, "v1"), A)
    host.approve(d, first, approver=OP)
    second = _send(d, _kit(tmp_path, site, "v2", views={"extra": ONE}), A)
    host.approve(d, second, approver=OP)
    assert "extra_rows" in _verbs(d, site)

    back = host.rollback(d, first, site=site, approver=OP)
    assert back["state"] == "applied"
    states = {b["id"]: b["state"] for b in host.review(d, site=site)}
    assert states[first] == "applied" and states[second] == "superseded"
    assert "extra_rows" not in _verbs(d, site), "the rolled-back bundle's objects are what is served"
    with pytest.raises(KitError, match="is for site"):
        host.rollback(d, first, site="another-site", approver=OP)


def test_a_contributor_cannot_replace_anothers_objects_without_an_override(d, tmp_path):
    site = "own-site"
    a = _send(d, _kit(tmp_path, site, "a"), A)
    host.approve(d, a, approver=OP)
    b_own = _send(d, _kit(tmp_path, site, "b", views={"b_view": ONE}, template=False), B)
    host.approve(d, b_own, approver=OP)
    assert {"temperature_rise_by_day", "b_view_rows"} <= _verbs(d, site), \
        "B's bundle added B's objects and left A's in place"

    clash = _send(d, _kit(tmp_path, site, "b2", views={"b_view": ONE}), B)  # carries A's names too
    with pytest.raises(KitError, match="another contributor's objects"):
        host.approve(d, clash, approver=OP)
    assert {b["id"]: b["state"] for b in host.review(d, site=site)}[clash] == "checked"
    host.approve(d, clash, approver=OP, override=True)
    owners = {(o["kind"], o["name"]): o["principal"] for o in host.owners(d, site=site)}
    assert owners[("verb", "temperature_rise_by_day")] == B
    assert owners[("gold", "b_view")] == B


def test_retire_is_restrict_by_default_and_force_cascades_keeping_the_record(d, tmp_path):
    site = "ret-site"
    a = _send(d, _kit(tmp_path, site, "a"), A)
    host.approve(d, a, approver=OP)
    with connect(d) as conn:
        conn.execute("CREATE VIEW public.leans_on_the_kit AS SELECT * FROM gold_ret_site.daily_stats")
    with pytest.raises(host.RetireRefused) as refused:
        host.retire(d, site, by=OP)
    assert "public.leans_on_the_kit" in refused.value.plan["dependents"]
    assert "gold_ret_site.daily_stats" in refused.value.plan["would_drop"]
    assert _verbs(d, site), "a refused retire drops nothing"

    done = host.retire(d, site, by=OP, force=True)
    assert done["dependents"] == ["public.leans_on_the_kit"] and _verbs(d, site) == set()
    with connect(d) as conn:
        assert conn.execute("SELECT to_regclass('public.leans_on_the_kit')").fetchone()[0] is None
        (action,) = conn.execute(
            f"SELECT action FROM {host.SCHEMA}.events WHERE site = %s ORDER BY id DESC LIMIT 1", (site,)).fetchone()
    assert action == "retire"
    assert [b["state"] for b in host.review(d, site=site)] == ["retired"], "the bundle stays as the record"


def test_withdraw_is_a_request_and_retire_by_principal_removes_only_theirs(d, tmp_path, monkeypatch):
    from fastapi import FastAPI, Request
    from fastapi.testclient import TestClient

    from axiom.extensions.builtins.data_platform import read_api
    from axiom.infra.site_authority import bind_caller_site, unbind_caller_site

    site = "wd-site"
    host.approve(d, _send(d, _kit(tmp_path, site, "a"), A), approver=OP)
    host.approve(d, _send(d, _kit(tmp_path, site, "b", views={"b_view": ONE}, template=False), B),
                 approver=OP)

    monkeypatch.setenv("DP1_RAG_DSN", d)
    monkeypatch.setattr(read_api, "holds_medallion", lambda: True)
    app = FastAPI()

    @app.middleware("http")
    async def gate(request: Request, call_next):
        request.state.principal = B
        token = bind_caller_site(site)
        try:
            return await call_next(request)
        finally:
            unbind_caller_site(token)

    app.include_router(read_api.build_data_read_router())
    door = TestClient(app)
    asked = door.post("/data/submit", json={"verb": "data.kit_withdraw",
                                            "params": {"site": site, "reason": "moving on"}}).json()
    assert asked["ok"], asked
    assert door.post("/data/query", json={"verb": "data.kit_withdraw", "params": {}}).status_code == 400, \
        "a withdraw is a write: never a read"
    elsewhere = door.post("/data/submit", json={"verb": "data.kit_withdraw",
                                                "params": {"site": "own-site"}}).json()
    assert not elsewhere["ok"] and "only withdraw from that site" in elsewhere["errors"][0]
    assert "b_view_rows" in _verbs(d, site), "a withdraw drops nothing by itself"
    pending = host.withdrawals(d, site=site)
    assert [(w["principal"], w["state"]) for w in pending] == [(B, "pending")]

    done = host.retire(d, site, by=OP, principal=B)
    assert done["withdrawals_applied"] == 1
    assert "b_view_rows" not in _verbs(d, site) and "temperature_rise_by_day" in _verbs(d, site)
    with connect(d) as conn:
        assert conn.execute("SELECT to_regclass('gold_wd_site.b_view')").fetchone()[0] is None
        assert conn.execute("SELECT to_regclass('gold_wd_site.daily_stats')").fetchone()[0] is not None
    assert host.withdrawals(d, site=site) == []


def test_retire_by_principal_refuses_when_another_contributors_view_reads_its_view(d, tmp_path):
    site = "dep-site"
    host.approve(d, _send(d, _kit(tmp_path, site, "a"), A), approver=OP)
    leaning = _kit(tmp_path, site, "b", template=False,
                   views={"on_top": "SELECT * FROM gold_dep_site.daily_stats\n"})
    host.approve(d, _send(d, leaning, B), approver=OP)
    with pytest.raises(host.RetireRefused, match="gold_dep_site.on_top"):
        host.retire(d, site, by=OP, principal=A)
    assert "temperature_rise_by_day" in _verbs(d, site), "nothing of A's was dropped"
