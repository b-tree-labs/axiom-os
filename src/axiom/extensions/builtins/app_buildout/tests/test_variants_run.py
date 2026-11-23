# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""VAR-A1: ``axi app run --variant axiom|product|site`` starts the variant on its
port and path; ``--all`` starts three under the supervisor.

The supervisor is the platform's one (``axiom.infra.switch.Supervisor``); these
tests replace it with a recorder so nothing binds a port, and prove what each
supervised copy is started with.
"""

from __future__ import annotations

import json
import logging

import pytest

from axiom.extensions.builtins.app_buildout import cli
from axiom.extensions.builtins.app_buildout.skills import variants as skill
from axiom.infra import variants as v
from axiom.infra.skills import SkillContext, SkillRegistry


class _Recorder:
    made: list[_Recorder] = []

    def __init__(self, argv, *, env, host, port, drain_s, ready_timeout_s, subject_prefix):
        self.argv, self.env, self.host, self.port = list(argv), dict(env), host, port
        self.subject_prefix = subject_prefix
        self.started = self.stopped = False
        _Recorder.made.append(self)

    def start(self):
        self.started = True

    def stop(self):
        self.stopped = True


@pytest.fixture
def recorded(monkeypatch, tmp_path):
    _Recorder.made = []
    ran: list[list] = []
    monkeypatch.setattr(skill, "_SUPERVISOR", _Recorder)
    monkeypatch.setattr(skill, "_RUN_GROUP", lambda sups: ran.append(list(sups)))
    monkeypatch.setenv(v.CONFIG_ENV, str(tmp_path / "absent-variants.toml"))
    monkeypatch.delenv(v.VARIANT_ENV, raising=False)
    return ran


def _ctx():
    return SkillContext(registry=SkillRegistry(), state_dir=None, logger=logging.getLogger("t"))


def test_var_a1_the_three_records_carry_the_decided_ports_and_paths(recorded):
    table = v.load_variants({})
    assert [(n, table[n].port, table[n].path) for n in v.NAMES] == [
        ("axiom", 8960, "/v/axiom/"),
        ("product", 8961, "/v/product/"),
        ("site", 8962, "/"),
    ]
    assert {table[n].cookie for n in v.NAMES} == {"axiom_s_axiom", "axiom_s_product", "axiom_s_site"}


def test_var_a1_a_deployment_record_overrides_a_default(tmp_path):
    cfg = tmp_path / "variants.toml"
    cfg.write_text(
        '[variant.product]\npath = "/v/example-product/"\nrealm = "example-bare"\n'
        'tenants = ["variant-example"]\n[variant.site.flags]\nlibrary = "on"\n'
    )
    table = v.load_variants(cfg)
    assert table["product"].path == "/v/example-product/"
    assert table["product"].tenant == "variant-example"
    assert table["site"].flags == {"library": "on"}


@pytest.mark.parametrize("field,value", [("port", 8960), ("cookie", "axiom_s_axiom"),
                                         ("realm", "axiom-bare"), ("path", "/v/axiom/")])
def test_var_a1_two_variants_sharing_what_separates_them_are_refused(field, value):
    with pytest.raises(v.VariantError, match=field):
        v.load_variants({"variant": {"product": {field: value}}})


def test_var_a1_one_variant_starts_on_its_port_with_its_realm(recorded):
    res = skill.run({"variant": "product"}, _ctx())
    assert res.ok, res.errors
    [sup] = _Recorder.made
    assert sup.started and sup.port == 8961 and sup.subject_prefix == "app:product"
    assert sup.argv[1:3] == ["-m", "axiom.extensions.builtins.http.cli"]
    assert sup.env[v.VARIANT_ENV] == "product"
    assert sup.env[v.SESSION_REALM_ENV] == "product-bare"
    assert sup.env[v.SESSION_COOKIE_ENV] == "axiom_s_product"
    assert sup.env[v.BASE_PATH_ENV] == "/v/product/"
    assert recorded == [[sup]]


def test_var_a1_all_starts_three_under_one_supervision_loop(recorded):
    res = skill.run({"all": True}, _ctx())
    assert res.ok, res.errors
    assert [s.port for s in _Recorder.made] == [8960, 8961, 8962]
    assert all(s.started for s in _Recorder.made)
    assert len(recorded) == 1 and [s.port for s in recorded[0]] == [8960, 8961, 8962]
    # Only the site takes over existing sessions in place.
    assert [s.env.get(v.SESSION_LEGACY_ENV) for s in _Recorder.made] == [None, None, "1"]


def test_var_a1_a_variant_that_fails_to_start_stops_the_ones_already_up(recorded, monkeypatch):
    class _Fails(_Recorder):
        def start(self):
            if self.port == 8961:
                raise RuntimeError("first copy never became ready")
            super().start()

    monkeypatch.setattr(skill, "_SUPERVISOR", _Fails)
    res = skill.run({"all": True}, _ctx())
    assert not res.ok and "product" in res.errors[0]
    assert _Recorder.made[0].stopped and recorded == []


def test_var_a1_dry_run_plans_and_starts_nothing(recorded, capsys):
    assert cli.main(["run", "--all", "--dry-run", "--json"]) == 0
    plan = json.loads(capsys.readouterr().out)["plan"]
    assert [(p["name"], p["url"], p["path"]) for p in plan] == [
        ("axiom", "http://127.0.0.1:8960/", "/v/axiom/"),
        ("product", "http://127.0.0.1:8961/", "/v/product/"),
        ("site", "http://127.0.0.1:8962/", "/"),
    ]
    assert _Recorder.made == [] and recorded == []


def test_var_a1_variants_lists_the_records(recorded, capsys):
    assert cli.main(["variants", "--json"]) == 0
    names = [r["name"] for r in json.loads(capsys.readouterr().out)["variants"]]
    assert names == ["axiom", "product", "site"]


def test_var_a1_a_variant_is_required(recorded):
    res = skill.run({}, _ctx())
    assert not res.ok and "--variant" in res.errors[0]


def test_var_a1_a_named_variant_that_does_not_exist_is_refused(monkeypatch, recorded):
    monkeypatch.setenv(v.VARIANT_ENV, "nope")
    with pytest.raises(v.VariantError):
        v.active_variant()
