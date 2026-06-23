# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Regression test for the 2026-05-31 self-hosted-node failure:
``load_definitions()`` was returning ``None`` because the module-level
``definitions = None`` in ``defs.py`` shadowed the ``__getattr__``
lazy-build path, so Dagster failed with ``DagsterInvariantViolation:
Loadable attributes must be either a JobDefinition, GraphDefinition,
Definitions, or RepositoryDefinition. Got None.``
"""

from __future__ import annotations

import pytest

pytest.importorskip("dagster")


def test_load_definitions_returns_definitions_not_none():
    from dagster import Definitions

    from axiom.extensions.builtins.data_platform.dagster_app import (
        load_definitions,
    )

    defs = load_definitions()
    assert defs is not None, (
        "load_definitions() returned None — this is the exact bug that "
        "broke the dagster code-server load on a self-hosted node (2026-05-31). "
        "Calling _build_definitions() directly is required; the "
        "`from .defs import definitions` path resolves to the module-"
        "level `None` and never hits __getattr__."
    )
    assert isinstance(defs, Definitions), (
        f"load_definitions() must return Definitions, got {type(defs)}"
    )


def test_load_definitions_exposes_box_corpus_sensor():
    """The DP-1 sensor must be present in the loaded Definitions."""
    from axiom.extensions.builtins.data_platform.dagster_app import (
        load_definitions,
    )

    defs = load_definitions()
    sensor_names = {s.name for s in defs.sensors}
    assert "box_corpus_sensor" in sensor_names, (
        f"box_corpus_sensor missing from Definitions; got: {sensor_names}"
    )
