# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""ADR-065 PR-1: JSON Schema entry path for ``axiom.infra.config``.

Tests cover the structural floor: schema load, property-type mapping,
default propagation, ``x-reloadable`` annotation carry-through, and
malformed-input rejection. Cross-field validators are the Pydantic
ceiling (PR-2); validation against a config document is exercised in
``test_config_cli.py``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from axiom.infra.config import (
    SchemaError,
    get_value,
    register_schema_from_jsonschema,
)
from axiom.infra.config import registry as registry_mod


@pytest.fixture(autouse=True)
def _clean_registry():
    registry_mod.reset_for_testing()
    yield


def _write_schema(tmp_path: Path, body: dict) -> Path:
    p = tmp_path / "config.schema.json"
    p.write_text(json.dumps(body), encoding="utf-8")
    return p


def test_register_schema_from_jsonschema_happy_path(tmp_path):
    schema_path = _write_schema(
        tmp_path,
        {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "type": "object",
            "required": ["owner"],
            "properties": {
                "owner": {"type": "string", "default": "@system:local"},
                "threshold": {"type": "integer", "default": 10},
                "enabled": {"type": "boolean", "default": True},
            },
        },
    )
    schema = register_schema_from_jsonschema("demo_ext", schema_path)

    assert schema["type"] == "object"
    assert get_value("demo_ext.owner") == "@system:local"
    assert get_value("demo_ext.threshold") == 10
    assert get_value("demo_ext.enabled") is True


def test_register_schema_handles_array_and_object_properties(tmp_path):
    schema_path = _write_schema(
        tmp_path,
        {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "type": "object",
            "properties": {
                "recipients": {"type": "array", "default": []},
                "meta": {"type": "object", "default": {}},
            },
        },
    )
    register_schema_from_jsonschema("demo_ext", schema_path)
    assert get_value("demo_ext.recipients") == []
    assert get_value("demo_ext.meta") == {}


def test_x_reloadable_annotation_preserved_in_description(tmp_path):
    schema_path = _write_schema(
        tmp_path,
        {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "type": "object",
            "properties": {
                "recipients": {
                    "type": "array",
                    "default": [],
                    "x-reloadable": True,
                    "description": "Hot-reloadable list",
                },
                "owner": {
                    "type": "string",
                    "default": "@system:local",
                    "x-reloadable": False,
                },
            },
        },
    )
    register_schema_from_jsonschema("demo_ext", schema_path)
    fields = {f.name: f for f in registry_mod.get_registry().fields()}
    assert "[x-reloadable=true]" in fields["demo_ext.recipients"].description
    assert "[x-reloadable=false]" in fields["demo_ext.owner"].description


def test_malformed_json_rejected(tmp_path):
    p = tmp_path / "bad.schema.json"
    p.write_text("{not json", encoding="utf-8")
    with pytest.raises(SchemaError) as exc:
        register_schema_from_jsonschema("demo_ext", p)
    assert "invalid JSON" in str(exc.value)


def test_top_level_must_be_object(tmp_path):
    schema_path = _write_schema(
        tmp_path,
        {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "type": "array",
        },
    )
    with pytest.raises(SchemaError):
        register_schema_from_jsonschema("demo_ext", schema_path)


def test_unknown_property_type_rejected(tmp_path):
    schema_path = _write_schema(
        tmp_path,
        {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "type": "object",
            "properties": {
                "bad": {"type": "blob"},
            },
        },
    )
    with pytest.raises(SchemaError) as exc:
        register_schema_from_jsonschema("demo_ext", schema_path)
    assert "unknown type" in str(exc.value)


def test_missing_file_raises_schema_error(tmp_path):
    with pytest.raises(SchemaError):
        register_schema_from_jsonschema("demo_ext", tmp_path / "nope.json")


def test_existing_register_schema_still_works(tmp_path):
    """The Python-dict entry path is unchanged (regression guard)."""
    from axiom.infra.config import register_schema

    register_schema("legacy_ext", {"name": str, "count": int})
    register_schema_from_jsonschema(
        "demo_ext",
        _write_schema(
            tmp_path,
            {
                "$schema": "https://json-schema.org/draft/2020-12/schema",
                "type": "object",
                "properties": {"flag": {"type": "boolean", "default": False}},
            },
        ),
    )
    fields = {f.name for f in registry_mod.get_registry().fields()}
    assert "legacy_ext.name" in fields
    assert "legacy_ext.count" in fields
    assert "demo_ext.flag" in fields
