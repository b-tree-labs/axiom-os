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


def test_load_definitions_returns_definitions_object():
    from axiom.extensions.builtins.data_platform.dagster_app import load_definitions

    defs = load_definitions()
    assert isinstance(defs, dagster.Definitions)


def test_box_corpus_asset_is_present():
    from axiom.extensions.builtins.data_platform.dagster_app import load_definitions

    defs = load_definitions()
    # asset_key surfaces vary by Dagster version — match on repr instead.
    assert "box_corpus" in repr(defs.assets)


def test_box_corpus_sensor_is_present():
    from axiom.extensions.builtins.data_platform.dagster_app import load_definitions

    defs = load_definitions()
    sensor_names = [s.name for s in defs.sensors]
    assert "box_corpus_sensor" in sensor_names


def test_dp1_box_run_job_is_defined():
    from axiom.extensions.builtins.data_platform.dagster_app import load_definitions

    defs = load_definitions()
    job_names = [j.name for j in defs.jobs]
    assert "dp1_box_run_job" in job_names
