# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0

"""The shared capability projector (ADR-072 / AEOS §4.9).

One canonical name round-trip, one inputs→JSON-schema derivation, one
side-effects→approval mapping — consumed by the CLI, MCP, and agent-tool
surfaces so none of them invents its own.
"""

from __future__ import annotations

import pytest

from axiom.infra.capability_projection import (
    approval_category,
    capability_to_surface_name,
    inputs_to_json_schema,
    is_read_only,
    surface_to_capability_name,
)
from axiom.infra.orchestrator.actions import ActionCategory
from axiom.infra.skills import SkillSpec


def _spec(name="press.draft", inputs=None, **kw):
    return SkillSpec(name=name, fn=lambda p, c: None, inputs=inputs or {}, **kw)


# --- naming round-trip ---------------------------------------------------- #
def test_name_roundtrip():
    assert capability_to_surface_name("press.draft") == "press__draft"
    assert surface_to_capability_name("press__draft") == "press.draft"


def test_name_roundtrip_is_lossless():
    for name in ("a.b", "data.reindex", "x.y.z"):
        assert surface_to_capability_name(capability_to_surface_name(name)) == name


def test_surface_name_is_llm_safe():
    # ^[A-Za-z0-9_-]+$ — no dots.
    assert "." not in capability_to_surface_name("a.b.c")


# --- schema derivation ---------------------------------------------------- #
def test_inputs_to_schema_maps_types():
    schema = inputs_to_json_schema({"source": "Path", "copies": "int", "ok": "bool"})
    assert schema["type"] == "object"
    assert schema["properties"]["source"]["type"] == "string"  # Path → string
    assert schema["properties"]["copies"]["type"] == "integer"
    assert schema["properties"]["ok"]["type"] == "boolean"


def test_empty_inputs_gives_empty_object():
    assert inputs_to_json_schema({}) == {"type": "object", "properties": {}}


def test_unknown_shape_defaults_to_string():
    schema = inputs_to_json_schema({"x": "SomethingWeird"})
    assert schema["properties"]["x"]["type"] == "string"


# --- approval reads from the capability, not the surface ------------------ #
def test_side_effects_false_is_read():
    spec = _spec(side_effects=False)
    assert is_read_only(spec) is True
    assert approval_category(spec) is ActionCategory.READ


def test_side_effects_true_is_write():
    spec = _spec(side_effects=True)
    assert is_read_only(spec) is False
    assert approval_category(spec) is ActionCategory.WRITE


def test_undeclared_side_effects_defaults_write():
    # Conservative: a capability that hasn't declared side_effects is
    # confirm-gated until it does (no silent auto-approve).
    spec = _spec()
    assert spec.side_effects is None
    assert approval_category(spec) is ActionCategory.WRITE


@pytest.mark.parametrize("name", ["press.draft", "data.reindex"])
def test_spec_carries_new_fields(name):
    spec = _spec(name=name, side_effects=False, idempotent=True)
    assert spec.side_effects is False
    assert spec.idempotent is True
