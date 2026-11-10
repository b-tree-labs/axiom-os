# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""The variant layer of flag resolution.

MVP-12-A1: precedence default → env → variant → node → account → user, with
``protected_in`` honoured. MVP-12-A4: each variant resolves its own flag set,
and an override in one does not reach another. Run against the real override
store on a private SQLite database (the conftest's ``sqlite_store``).
"""

from __future__ import annotations

import pytest

from axiom.extensions.builtins.flags.service import FlagService
from axiom.infra import variants as v


@pytest.fixture(autouse=True)
def _variants(tmp_path, monkeypatch):
    cfg = tmp_path / "variants.toml"
    cfg.write_text(
        '[variant.axiom.flags]\nhistory_timeline = "on"\nbeta_planner = "on"\n'
        '[variant.product.flags]\nhistory_timeline = "soon"\n'
    )
    monkeypatch.setenv(v.CONFIG_ENV, str(cfg))
    monkeypatch.delenv(v.VARIANT_ENV, raising=False)


def _in(monkeypatch, name):
    monkeypatch.setenv(v.VARIANT_ENV, name)


def test_mvp_12_a1_the_variant_layer_sits_between_env_and_node(monkeypatch, sqlite_store):
    svc = FlagService(sqlite_store)
    _in(monkeypatch, "axiom")
    monkeypatch.setenv("AXIOM_FLAG_HISTORY_TIMELINE", "off")
    r = svc.resolve(node="node-a")
    assert (r.flags["history_timeline"], r.sources["history_timeline"]) == ("on", "variant")
    svc.set_override("node", "node-a", "history_timeline", "soon", by="@admin:x")
    r = svc.resolve(node="node-a", user="u1")
    assert (r.flags["history_timeline"], r.sources["history_timeline"]) == ("soon", "node")
    svc.set_override("user", "u1", "history_timeline", "off", by="@u1:x")
    assert svc.resolve(node="node-a", user="u1").flags["history_timeline"] == "off"


def test_mvp_12_a1_a_protected_flag_takes_the_variant_layer_and_no_stored_override(
        monkeypatch, sqlite_store):
    monkeypatch.setenv("AXIOM_ENVIRONMENT", "production")
    _in(monkeypatch, "axiom")
    svc = FlagService(sqlite_store)
    r = svc.resolve(node="node-a")
    assert (r.flags["beta_planner"], r.sources["beta_planner"]) == ("on", "variant")
    assert svc.resolve_flag("beta_planner", node="node-a") == "on"


@pytest.mark.parametrize("name,expected", [("axiom", "on"), ("product", "soon"), ("site", "off")])
def test_mvp_12_a4_each_variant_resolves_its_own_flag_set(monkeypatch, sqlite_store, name, expected):
    _in(monkeypatch, name)
    assert FlagService(sqlite_store).resolve().flags["history_timeline"] == expected


@pytest.mark.parametrize("home,other", [(a, b) for a in v.NAMES for b in v.NAMES if a != b])
def test_mvp_12_a4_an_override_in_one_variant_does_not_reach_another(
        monkeypatch, sqlite_store, home, other):
    svc = FlagService(sqlite_store)
    _in(monkeypatch, other)
    before = svc.resolve(node="node-a", account="acct", user="u1").flags["library"]
    _in(monkeypatch, home)
    for scope, sid in (("node", "node-a"), ("account", "acct"), ("user", "u1")):
        svc.set_override(scope, sid, "library", "off" if before != "off" else "on", by="@admin:x")
    assert svc.resolve(node="node-a", account="acct", user="u1").flags["library"] != before
    _in(monkeypatch, other)
    assert svc.resolve(node="node-a", account="acct", user="u1").flags["library"] == before
    assert svc.resolve_flag("library", node="node-a", account="acct", user="u1") == before


def test_mvp_12_a4_the_in_place_variant_keeps_the_installs_overrides(monkeypatch, sqlite_store):
    """The site takes the install over in place: what was set before it still applies."""
    FlagService(sqlite_store, variant=None).set_override("node", "node-a", "library", "on", by="@a:x")
    _in(monkeypatch, "site")
    assert FlagService(sqlite_store).resolve(node="node-a").flags["library"] == "on"
    _in(monkeypatch, "axiom")
    assert FlagService(sqlite_store).resolve(node="node-a").flags["library"] == "soon"


def test_mvp_12_a4_a_plain_install_has_no_variant_layer(sqlite_store):
    r = FlagService(sqlite_store).resolve()
    assert r.flags["history_timeline"] == "off" and r.sources["history_timeline"] == "default"
