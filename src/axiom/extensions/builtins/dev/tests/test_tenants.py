# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tenants: real gate accounts per persona, bound to the tenant's site, on its own host."""

from __future__ import annotations

import pytest

from axiom.extensions.builtins.dev import config, tenants
from axiom.extensions.builtins.dev.skills import as_, up
from axiom.webauth import load_user_records

from .conftest import Ctx, FakeVault, _git_checkout


def _write(root, extra=""):
    (root / "dev.toml").write_text(
        '[[tenant]]\nid = "alpha"\nsite = "alpha-site"\n[[tenant]]\nid = "beta"\n' + extra
    )


def test_tenants_parse_with_the_site_defaulting_to_the_id(tmp_path):
    _write(tmp_path)
    cfg = config.load(tmp_path)
    assert [(t.id, t.site) for t in cfg.tenants] == [("alpha", "alpha-site"), ("beta", "beta")]


def test_duplicate_tenants_are_refused(tmp_path):
    (tmp_path / "dev.toml").write_text('[[tenant]]\nid="a"\n[[tenant]]\nid="a"\n')
    with pytest.raises(config.DevConfigError):
        config.load(tmp_path)


def test_a_missing_site_checkout_says_how_to_get_it(tmp_path):
    t = config.Tenant(id="alpha", site="alpha", config="../alpha-site")
    with pytest.raises(tenants.TenantProblem, match="AXI_DEV_SITES"):
        tenants.resolve_config(tmp_path, t, env={})


def test_a_site_checkout_is_found_under_the_sites_dir(tmp_path):
    (tmp_path / "sites" / "alpha-site").mkdir(parents=True)
    t = config.Tenant(id="alpha", site="alpha", config="../alpha-site")
    got = tenants.resolve_config(tmp_path / "me", t, env={"AXI_DEV_SITES": str(tmp_path / "sites")})
    assert got == tmp_path / "sites" / "alpha-site"


def test_a_persona_may_only_map_to_a_role_the_gate_has(tmp_path):
    _write(tmp_path, '[personas]\nsuperuser = "root-of-all"\n')
    with pytest.raises(tenants.TenantProblem, match="root-of-all"):
        tenants.personas_for(config.load(tmp_path))


def test_accounts_are_real_gate_accounts_bound_to_their_site(tmp_path):
    _write(tmp_path)
    cfg = config.load(tmp_path)
    vault = FakeVault()
    made = tenants.ensure_accounts(accounts=tmp_path / "users.json", lane="l", cfg=cfg, vault=vault)
    rows = {r["email"]: r for r in load_user_records(tmp_path / "users.json")}
    assert len(made) == 2 * len(cfg.personas) == len(rows)
    op = rows[tenants.email_for("alpha", "site-admin")]
    assert op["site"] == "alpha-site" and list(op["roles"]) == ["site-admin"]
    assert rows[tenants.email_for("beta", "visitor")]["roles"] == ("guest",)
    assert "sample" in rows[tenants.email_for("beta", "auditor")]["name"].lower()
    assert all(vault.exists(m["secret"]) for m in made)  # passwords only in the vault
    again = tenants.ensure_accounts(
        accounts=tmp_path / "users.json", lane="l", cfg=cfg, vault=vault
    )
    assert again == made  # idempotent


def test_up_bounds_the_node_to_the_declared_sites_and_dev_as_names_the_host(
    tmp_path, lanes, stubbed_node, monkeypatch
):
    root = _git_checkout(tmp_path / "site")
    _write(root)
    ctx = Ctx(tmp_path / "state")
    r = up.run({"root": str(root), "email": "a@example.org", "sign_in": "none"}, ctx)
    assert r.ok, r.errors
    assert stubbed_node[0].env["AXIOM_SERVED_SITES"] == "alpha-site,beta"
    api = lanes.get("site").api
    assert r.value["tenants"][0]["url"] == f"http://alpha.localhost:{api}"
    from axiom.extensions.builtins.dev import node

    monkeypatch.setattr(node, "read_record", lambda home: {"pid": 1})
    who = as_.run({"root": str(root), "target": "beta:auditor"}, ctx)
    assert who.ok, who.errors
    assert who.value["url"] == f"http://beta.localhost:{api}/" and who.value["role"] == "viewer"
    assert not as_.run({"root": str(root), "target": "gamma"}, ctx).ok
