# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""A credential bound to a tenant site cannot read another tenant through the gold verbs.

``authorize_site`` holds a read that NAMES a site to the caller's own. The generic
gold verbs (``aggregate``, ``series``, ``compare``, ``roles``, ``uncertainty_coverage``)
take a table and a filter instead, and name their site INSIDE the filter text, or
omit it. Measured on a real database: a credential bound to one tenant counted the
other tenant's rows by writing ``site = '<other>'``, and counted every tenant's by
writing nothing. The argument-level check never saw either.

Real TimescaleDB, two tenants and a home site, the verbs called as the platform
calls them. The negative control is the operator, who must still read everything.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path

import psycopg
import pytest

from axiom.extensions.builtins.data_platform import gold_query as gq
from axiom.extensions.builtins.data_platform.conformance import (
    GOLD_SIGNALS_DDL,
    SILVER_DDL,
    pg_upsert,
)
from axiom.extensions.builtins.data_platform.skills import gold, medallion
from axiom.extensions.builtins.data_platform.tests import timescale_container
from axiom.infra import site_authority as sa
from axiom.infra.principal import PrincipalContext
from axiom.infra.skills import SkillContext, SkillRegistry

T0 = datetime(2026, 10, 1, tzinfo=UTC)
SITES = {"tenant-a": 40, "tenant-b": 25, "home-site": 10}


@pytest.fixture(scope="module")
def dsn():
    name, url = timescale_container.start()
    with psycopg.connect(url, autocommit=True) as c:
        c.execute("DROP SCHEMA IF EXISTS gold CASCADE")
        c.execute("DROP SCHEMA IF EXISTS silver CASCADE")
        for stmt in SILVER_DDL + GOLD_SIGNALS_DDL:
            c.execute(stmt)
        cur = c.cursor()
        up = pg_upsert(cur)
        for site, n in SITES.items():
            for i in range(n):
                up(
                    {
                        "site": site, "feed": "f", "channel": "temp",
                        "ts": T0 + timedelta(minutes=i), "value": 20.0 + i,
                        "unit": "degC", "row_hash": f"{site}-{i}", "schema_ref": "x/v1",
                        "role": "temperature", "source_class": "measured",
                    }
                )
    yield url
    timescale_container.stop(name)


@pytest.fixture(autouse=True)
def _env(monkeypatch, dsn, tmp_path):
    monkeypatch.setenv("DP1_RAG_DSN", dsn)
    monkeypatch.setenv("AXIOM_SITE", "home-site")
    monkeypatch.delenv("AXIOM_HOME_SITES", raising=False)
    monkeypatch.setenv("AXIOM_CONFIG_DIR", str(tmp_path))  # no serving policy file
    monkeypatch.setattr(gold, "_GUARD", None)
    sa._reset_default_site_context()
    yield
    sa._reset_default_site_context()


def _ctx(tmp_path: Path, handle: str) -> SkillContext:
    return SkillContext(
        registry=SkillRegistry(), state_dir=tmp_path, logger=logging.getLogger("t"),
        principal=PrincipalContext(handle=handle), surface="mcp",
    )


class bound:
    """The credential's site, as the HTTP authz middleware binds it."""

    def __init__(self, site: str | None, *, every_site: bool = False) -> None:
        self.site = site
        self.every_site = every_site

    def __enter__(self):
        self._tok = sa.bind_caller_site(self.site, every_site=self.every_site)

    def __exit__(self, *exc):
        sa.unbind_caller_site(self._tok)


def _count(tmp_path, handle, **extra):
    r = gold.aggregate(
        {"table": "signals", "column": "value", "fn": "count", **extra}, _ctx(tmp_path, handle)
    )
    return r


def test_a_tenant_counting_with_no_filter_counts_only_its_own_site(tmp_path):
    with bound("tenant-a"):
        r = _count(tmp_path, "@r:tenant-a")
    assert r.ok, r.errors
    assert r.value["data"]["value"] == SITES["tenant-a"], "not 75, the whole node"
    assert "held to the credential's site" in r.value["provenance"]["method"]


def test_a_tenant_naming_another_site_in_the_filter_is_refused_by_name(tmp_path):
    with bound("tenant-a"):
        for other in ("tenant-b", "home-site"):
            r = _count(tmp_path, "@r:tenant-a", filter=f"site = '{other}'")
            assert not r.ok
            assert "tenant-a" in r.errors[0] and other in r.errors[0]


def test_a_tenant_cannot_widen_with_an_in_list(tmp_path):
    with bound("tenant-a"):
        r = _count(tmp_path, "@r:tenant-a", filter="site in ('tenant-a', 'tenant-b')")
    assert not r.ok and "tenant-b" in r.errors[0]


def test_a_tenant_grouping_by_site_sees_one_group(tmp_path):
    with bound("tenant-a"):
        r = _count(tmp_path, "@r:tenant-a", group_by=["site"])
    assert r.ok, r.errors
    assert [g["site"] for g in r.value["data"]["groups"]] == ["tenant-a"]


def test_a_tenants_series_is_its_own_site_only(tmp_path):
    with bound("tenant-b"):
        r = gold.series(
            {
                "table": "signals", "column": "value", "bucket": "1 hour", "time_column": "ts",
                "fn": "count",
                "window": {"column": "ts", "start": T0.isoformat(), "end": (T0 + timedelta(days=1)).isoformat()},
            },
            _ctx(tmp_path, "@r:tenant-b"),
        )
    assert r.ok, r.errors
    total = sum(p["value"] for p in r.value["data"]["series"])
    assert total == SITES["tenant-b"]


def test_a_tenants_roles_name_only_its_own_site(tmp_path):
    with bound("tenant-a"):
        r = gold.roles({}, _ctx(tmp_path, "@r:tenant-a"))
    assert r.ok, r.errors
    (role,) = r.value["data"]["roles"]
    assert role["sites"] == ["tenant-a"] and role["rows"] == SITES["tenant-a"]


def test_a_tenants_comparison_cannot_name_another_site(tmp_path):
    with bound("tenant-a"):
        refused = gold.compare(
            {"role": "temperature", "bucket": "1 hour", "sites": "tenant-a,tenant-b"},
            _ctx(tmp_path, "@r:tenant-a"),
        )
        own = gold.compare(
            {"role": "temperature", "bucket": "1 hour"}, _ctx(tmp_path, "@r:tenant-a")
        )
    assert not refused.ok and "tenant-b" in refused.errors[0]
    assert own.ok, own.errors
    (unit,) = own.value["data"]["by_unit"].values()
    assert list(unit["sites"]) == ["tenant-a"]


def test_a_tenants_uncertainty_coverage_is_its_own_site(tmp_path):
    with bound("tenant-a"):
        ok = gold.uncertainty_coverage({}, _ctx(tmp_path, "@r:tenant-a"))
        bad = gold.uncertainty_coverage({"site": "tenant-b"}, _ctx(tmp_path, "@r:tenant-a"))
    assert ok.ok, ok.errors
    assert {s["site"] for s in ok.value["data"]["streams"]} <= {"tenant-a"}
    assert not bad.ok and "tenant-b" in bad.errors[0]


def test_the_serving_report_and_bronze_are_the_operators(tmp_path):
    with bound("tenant-a"):
        report = gold.serving_report({}, _ctx(tmp_path, "@r:tenant-a"))
        raw = medallion.catalog({"tier": "bronze"}, _ctx(tmp_path, "@r:tenant-a"))
    assert not report.ok and "operator" in report.errors[0]
    assert not raw.ok and "raw files" in raw.errors[0]


def test_the_operator_is_unchanged_and_reads_every_site(tmp_path):
    """Negative control: without it, 'tenants are held' and 'everything is broken' look alike."""
    # No credential binding (the terminal), and a credential holding the owner's grant.
    for site, every in ((None, False), ("home-site", True), ("tenant-a", True)):
        with bound(site, every_site=every):
            assert _count(tmp_path, "@op:home-site").value["data"]["value"] == sum(SITES.values())
            one = _count(tmp_path, "@op:home-site", filter="site = 'tenant-b'")
            assert one.value["data"]["value"] == SITES["tenant-b"]


def test_a_home_site_viewer_is_held_to_the_home_site(tmp_path):
    """ADR-203: the node's home site is no exemption. A viewer key minted for it
    counted every partner hosted beside it, readings included."""
    with bound("home-site"):
        r = _count(tmp_path, "@viewer:home-site")
        assert r.ok, r.errors
        assert r.value["data"]["value"] == SITES["home-site"]
        grouped = _count(tmp_path, "@viewer:home-site", group_by=["site"])
        assert grouped.ok, grouped.errors
        assert {g["site"] for g in grouped.value["data"]["groups"]} == {"home-site"}
        for other in ("tenant-a", "tenant-b"):
            refused = _count(tmp_path, "@viewer:home-site", filter=f"site = '{other}'")
            assert not refused.ok
            assert "home-site" in refused.errors[0] and other in refused.errors[0]


def test_a_tenant_is_refused_a_table_that_cannot_be_held_to_a_site():
    with pytest.raises(gq.TenantScopeError, match="no site column"):
        gq.scope_clause({"ts", "value"}, "tenant-a", table="rollup")


def test_the_scope_is_the_credentials_and_does_not_outlive_the_request():
    with bound("tenant-a"):
        assert sa.tenant_scope() == "tenant-a"
    assert sa.tenant_scope() is None
