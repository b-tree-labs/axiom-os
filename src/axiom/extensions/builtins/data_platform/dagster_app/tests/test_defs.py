# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Dagster wiring smoke tests.

Skipped unless the ``[data-platform]`` extra is installed
(``pip install "axiom-os-lm[data-platform]"``). When dagster IS
available these tests verify the Definitions object loads cleanly + the
sensor decorator-wrapped function is callable. They do not exercise a
real Box end-to-end — that's the deploy-runbook smoke."""

from __future__ import annotations

import pytest

dagster = pytest.importorskip("dagster")


@pytest.fixture(autouse=True)
def _isolate_state_dir(monkeypatch, tmp_path):
    """Point connector discovery at an empty dir so these env-path tests
    don't pick up connector TOMLs from the developer's real ~/.axi."""
    monkeypatch.setenv("AXI_STATE_DIR", str(tmp_path / "state"))


@pytest.fixture
def single_source(monkeypatch):
    """Configure one Box source (the legacy single-folder env path)."""
    monkeypatch.delenv("DP1_BOX_SOURCES", raising=False)
    monkeypatch.setenv("DP1_BOX_FOLDER_ID", "363592758132")
    monkeypatch.setenv("DP1_BOX_SOURCE_NAME", "research-corpus")


def test_load_definitions_returns_definitions_object(single_source):
    from axiom.extensions.builtins.data_platform.dagster_app import load_definitions

    defs = load_definitions()
    assert isinstance(defs, dagster.Definitions)


def test_corpus_asset_is_present(single_source):
    from axiom.extensions.builtins.data_platform.dagster_app import load_definitions

    defs = load_definitions()
    # asset_key surfaces vary by Dagster version — match on repr instead.
    assert "corpus__research_corpus" in repr(defs.assets)


def test_corpus_sensor_is_present(single_source):
    from axiom.extensions.builtins.data_platform.dagster_app import load_definitions

    defs = load_definitions()
    sensor_names = [s.name for s in defs.sensors]
    assert "corpus__research_corpus_sensor" in sensor_names


def test_dp1_run_job_is_defined(single_source):
    from axiom.extensions.builtins.data_platform.dagster_app import load_definitions

    defs = load_definitions()
    job_names = [j.name for j in defs.jobs]
    assert "dp1_run_job__research_corpus" in job_names


def test_multiple_sources_yield_independent_assets_and_sensors(monkeypatch):
    from axiom.extensions.builtins.data_platform.dagster_app import load_definitions

    monkeypatch.setenv(
        "DP1_BOX_SOURCES",
        '[{"name": "research-corpus", "folder_id": "363592758132", '
        '"default_tier": "rag-community"}, '
        '{"name": "dept-archive", "folder_id": "228326101313", '
        '"default_tier": "rag-org"}]',
    )

    defs = load_definitions()

    sensor_names = {s.name for s in defs.sensors}
    assert sensor_names == {
        "corpus__research_corpus_sensor",
        "corpus__ut_ne_archive_sensor",
    }
    # Two corpus assets + two readiness markers.
    assert "corpus__research_corpus" in repr(defs.assets)
    assert "corpus__ut_ne_archive" in repr(defs.assets)


def test_no_sources_configured_yields_empty_definitions(monkeypatch):
    from axiom.extensions.builtins.data_platform.dagster_app import load_definitions

    monkeypatch.delenv("DP1_BOX_SOURCES", raising=False)
    monkeypatch.delenv("DP1_BOX_FOLDER_ID", raising=False)

    defs = load_definitions()
    assert list(defs.sensors) == []


# NOTE (merge of RATIONALIZE / Box OAuth fix, 2026-06-30): main's
# `_build_box_server_auth` tests moved — the Dagster path no longer builds
# Box auth itself. The env shim maps BOX_CCG_CONFIG / BOX_JWT_CONFIG to a
# `jwt_secret_ref=env://<VAR>` param and the Box provider's
# `_resolve_jwt_auth` does the shape dispatch (OAuth / CCG / JWT). That
# regression is covered in test_box_sources.py (spec injection + dispatch
# through `_build_source`) and test_provider_jwt_wiring.py (SecretRef path).


# ---------------------------------------------------------------- catalog assets


class TestCatalogAssets:
    ROWS = (
        [  # datasets: (tier, full_name, object_type)
            ("gold", "gold.series_a", "VIEW"),
            ("silver", "silver.base", "TABLE"),
            ("bronze", "bronze.manifest", "TABLE"),
        ],
        [  # edges: (source, target, entity_type)
            ("silver.base", "gold.series_a", "view-dependency"),
            ("store:landing (files)", "bronze.manifest", "manifest"),
        ],
    )

    def test_specs_group_by_tier_with_deps_and_external_nodes(self):
        from axiom.extensions.builtins.data_platform.dagster_app.defs import (
            _catalog_asset_specs,
        )

        specs = _catalog_asset_specs(rows=self.ROWS)
        by_key = {tuple(sp.key.path): sp for sp in specs}
        assert by_key[("gold", "series_a")].group_name == "gold"
        assert [d.asset_key.path for d in by_key[("gold", "series_a")].deps] == [["silver", "base"]]
        # the external hop appears as a sources-group node feeding the manifest
        assert by_key[("store", "landing_files")].group_name == "sources"
        assert [d.asset_key.path for d in by_key[("bronze", "manifest")].deps] == [
            ["store", "landing_files"]
        ]

    def test_unreachable_catalog_yields_no_assets(self):
        from axiom.extensions.builtins.data_platform.dagster_app.defs import (
            _catalog_asset_specs,
        )

        assert _catalog_asset_specs(rows=(None, None)) == []

    def test_sanitize_key_shapes(self):
        from axiom.extensions.builtins.data_platform.dagster_app.defs import _sanitize_key

        assert _sanitize_key("gold.reactor_power") == ["gold", "reactor_power"]
        assert _sanitize_key("box:serial_data (Zoc CSVs)") == ["box", "serial_data_Zoc_CSVs"]
        assert _sanitize_key("files:~/.axi/bronze/<connector>") == ["files", "axi_bronze_connector"]

    def test_catalog_rows_falls_back_to_psycopg2(self, monkeypatch):
        """dagster-k8s images ship psycopg2 (Dagster's own PG storage driver),
        not psycopg 3 — the catalog read must work with either."""
        import sys
        import types

        from axiom.extensions.builtins.data_platform.dagster_app import defs

        calls = {}

        class _Cursor:
            def execute(self, sql):
                calls["last_sql"] = sql
                self._edges = "table_lineage" in sql

            def fetchall(self):
                if self._edges:
                    return [("silver.base", "gold.series_a", "view-dependency")]
                return [("gold", "gold.series_a", "VIEW", "curated")]

        class _Conn:
            def cursor(self):
                return _Cursor()

            def close(self):
                calls["closed"] = True

        fake_pg2 = types.ModuleType("psycopg2")
        fake_pg2.connect = lambda dsn, connect_timeout: _Conn()
        monkeypatch.setitem(sys.modules, "psycopg", None)  # import -> ImportError
        monkeypatch.setitem(sys.modules, "psycopg2", fake_pg2)

        datasets, edges = defs._catalog_rows("postgresql://x")
        assert datasets == [("gold", "gold.series_a", "VIEW")]
        assert edges == [("silver.base", "gold.series_a", "view-dependency")]
        assert calls["closed"] is True

    def test_system_visibility_rows_and_their_edges_are_hidden(self, monkeypatch):
        """Curation: visibility='system' datasets never reach the graph, and
        edges touching them are dropped so they cannot reappear as source nodes."""
        import sys
        import types

        from axiom.extensions.builtins.data_platform.dagster_app import defs

        class _Cursor:
            def execute(self, sql):
                self._edges = "table_lineage" in sql

            def fetchall(self):
                if self._edges:
                    return [
                        ("silver.base", "gold.series_a", "view-dependency"),
                        ("gold_staging.scratch", "gold.series_a", "view-dependency"),
                    ]
                return [
                    ("gold", "gold.series_a", "VIEW", "curated"),
                    ("gold", "gold_staging.scratch", "TABLE", "system"),
                    ("silver", "silver.base", "TABLE", "internal"),
                ]

        class _Conn:
            def cursor(self):
                return _Cursor()

            def close(self):
                pass

        fake = types.ModuleType("psycopg")
        fake.connect = lambda dsn, connect_timeout: _Conn()
        monkeypatch.setitem(sys.modules, "psycopg", fake)

        datasets, edges = defs._catalog_rows("postgresql://x")
        assert ("gold", "gold_staging.scratch", "TABLE") not in datasets
        assert len(datasets) == 2
        assert edges == [("silver.base", "gold.series_a", "view-dependency")]
