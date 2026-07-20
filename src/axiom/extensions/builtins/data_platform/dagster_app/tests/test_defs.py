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
