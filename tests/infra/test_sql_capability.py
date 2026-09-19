# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""SQL function signature → capability inputs (``axiom.infra.sql_capability``).

Pure parsing and projection: the output of ``pg_get_function_arguments``
plus ``obj_description`` becomes the same ``inputs`` shape map a
``SkillSpec`` carries, and that map projects through the one projector.
"""

from __future__ import annotations

import json

import pytest

from axiom.infra.capability_projection import inputs_to_json_schema
from axiom.infra.skills import SkillSpec
from axiom.infra.sql_capability import (
    PG_FUNCTION_CATALOG_SQL,
    CapabilityInputs,
    catalog_rows_to_specs,
    parse_pg_function_args,
    pg_type_to_input_type,
    split_top_level,
    sql_function_to_spec,
    strip_param_prefix,
)

SERIES_ARGS = "p_start date, p_end date DEFAULT NULL, p_limit integer DEFAULT 100"


# ---------------------------------------------------------------------------
# parse_pg_function_args
# ---------------------------------------------------------------------------


def test_parse_basic_with_defaults_and_prefix():
    parsed = parse_pg_function_args(SERIES_ARGS)
    assert parsed.types == {"start": "date", "end": "date", "limit": "integer"}
    assert parsed.optional == frozenset({"end", "limit"})
    assert parsed.variadic is None


def test_parse_preserves_declaration_order():
    parsed = parse_pg_function_args("p_z text, p_a text, p_m text")
    assert list(parsed.types) == ["z", "a", "m"]


def test_parse_empty_signature():
    assert parse_pg_function_args("") == ({}, frozenset(), None)
    assert parse_pg_function_args("   ").types == {}


def test_parse_prefix_configurable_and_not_forced():
    parsed = parse_pg_function_args("in_start date, p_end date", prefix="in_")
    assert parsed.types == {"start": "date", "p_end": "date"}
    assert parse_pg_function_args("p_start date", prefix="").types == {"p_start": "date"}


def test_parse_prefix_never_empties_a_name():
    assert strip_param_prefix("p_", "p_") == "p_"
    assert parse_pg_function_args("p_ text").types == {"p_": "text"}


def test_parse_duplicate_after_prefix_strip_is_explicit_error():
    with pytest.raises(ValueError, match="not unique"):
        parse_pg_function_args("p_x integer, x integer")


def test_parse_arrays_keep_pg_type():
    parsed = parse_pg_function_args("p_ids integer[], p_tags text[] DEFAULT '{}'::text[]")
    assert parsed.types == {"ids": "integer[]", "tags": "text[]"}
    assert parsed.optional == frozenset({"tags"})


def test_parse_quoted_identifiers():
    parsed = parse_pg_function_args('"Start Date" date, "p_weird""name" text DEFAULT \'a\'')
    assert parsed.types == {"Start Date": "date", 'weird"name': "text"}
    assert parsed.optional == frozenset({'weird"name'})


def test_parse_out_excluded_inout_kept():
    parsed = parse_pg_function_args("IN p_x integer, OUT total bigint, INOUT p_y text")
    assert parsed.types == {"x": "integer", "y": "text"}


def test_parse_variadic_handled():
    parsed = parse_pg_function_args("p_base text, VARIADIC p_ids integer[]")
    assert parsed.types == {"base": "text", "ids": "integer[]"}
    assert parsed.variadic == "ids"
    assert "ids" not in parsed.optional  # no DEFAULT → still required


def test_parse_defaults_with_commas_parens_and_keywords():
    args = (
        "p_a numeric(10,2) DEFAULT 1.5, "
        "p_b text DEFAULT 'x, y', "
        "p_c text[] DEFAULT ARRAY['a','b'], "
        "p_d timestamp with time zone DEFAULT now(), "
        "p_e text DEFAULT 'DEFAULT'"
    )
    parsed = parse_pg_function_args(args)
    assert parsed.types == {
        "a": "numeric(10,2)",
        "b": "text",
        "c": "text[]",
        "d": "timestamp with time zone",
        "e": "text",
    }
    assert parsed.optional == frozenset({"a", "b", "c", "d", "e"})


def test_parse_equals_default_form():
    parsed = parse_pg_function_args("p_limit integer = 10, p_name text")
    assert parsed.optional == frozenset({"limit"})
    assert parsed.types["name"] == "text"


def test_parse_unnamed_parameters_get_positional_names():
    assert parse_pg_function_args("integer, text").types == {"arg1": "integer", "arg2": "text"}
    assert parse_pg_function_args("double precision").types == {"arg1": "double precision"}
    assert parse_pg_function_args("p_x integer, timestamp with time zone").types == {
        "x": "integer",
        "arg2": "timestamp with time zone",
    }


def test_split_top_level_respects_quotes_and_brackets():
    assert split_top_level("a, b(c, d), 'e, f', g[1, 2]") == ["a", "b(c, d)", "'e, f'", "g[1, 2]"]


# ---------------------------------------------------------------------------
# pg_type_to_input_type
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "pg_type, expected",
    [
        ("text", "str"),
        ("character varying(20)", "str"),
        ("varchar", "str"),
        ("uuid", "str"),
        ("bytea", "str"),
        ("interval", "str"),
        ("integer", "int"),
        ("int4", "int"),
        ("bigint", "int"),
        ("smallint", "int"),
        ("pg_catalog.int8", "int"),
        ("real", "float"),
        ("double precision", "float"),
        ("numeric(10,2)", "float"),
        ("NUMERIC", "float"),
        ("boolean", "bool"),
        ("bool", "bool"),
        ("date", "date"),
        ("timestamp", "datetime"),
        ("timestamptz", "datetime"),
        ("timestamp with time zone", "datetime"),
        ("timestamp without time zone", "datetime"),
        ("json", "dict"),
        ("jsonb", "dict"),
        ("text[]", "list"),
        ("integer[][]", "list"),
        ("public.my_enum[]", "list"),
        ("anyarray", "list"),
        ("public.mood_enum", "str"),  # enum/domain → string
        ("my_composite", "str"),
    ],
)
def test_pg_type_mapping_table(pg_type, expected):
    assert pg_type_to_input_type(pg_type) == expected


def test_pg_type_vocabulary_is_what_the_projector_maps():
    """Every shape this module emits is a known key of the one projector."""
    known = {"str", "int", "float", "bool", "date", "datetime", "dict", "list"}
    for pg_type in ("text", "integer", "numeric", "bool", "date", "timestamptz", "jsonb", "text[]"):
        shape = pg_type_to_input_type(pg_type)
        assert shape in known
        assert inputs_to_json_schema({"x": shape})["properties"]["x"]["type"] != ""


# ---------------------------------------------------------------------------
# sql_function_to_spec → JSON Schema
# ---------------------------------------------------------------------------


def test_spec_record_round_trips_to_json_schema():
    spec = sql_function_to_spec("series", SERIES_ARGS, "Rows between two dates.")
    assert isinstance(spec, CapabilityInputs)
    assert spec.name == "series"
    assert spec.description == "Rows between two dates."
    assert spec.inputs == {"start": "date!", "end": "date", "limit": "int"}
    assert spec.optional == frozenset({"end", "limit"})
    assert spec.required == ("start",)

    schema = spec.to_json_schema()
    assert schema == inputs_to_json_schema(spec.inputs)
    assert schema["type"] == "object"
    assert schema["required"] == ["start"]
    assert schema["properties"]["start"] == {
        "type": "string",
        "format": "date",
        "description": "start (date)",
    }
    assert schema["properties"]["limit"]["type"] == "integer"
    assert "required" not in schema["properties"]["end"]


def test_spec_with_all_optional_has_no_required_key():
    spec = sql_function_to_spec("f", "p_a integer DEFAULT 1, p_b text DEFAULT ''")
    assert spec.required == ()
    assert "required" not in spec.to_json_schema()


def test_spec_feeds_a_skillspec_unchanged():
    """The record's ``inputs`` is a ``SkillSpec.inputs`` — no translation layer."""
    record = sql_function_to_spec("series", SERIES_ARGS, "Rows between two dates.")
    skill = SkillSpec(
        name="catalog.series",
        fn=lambda p, c: None,
        inputs=record.inputs,
        description=record.description,
    )
    assert inputs_to_json_schema(skill.inputs) == record.to_json_schema()


def test_spec_is_deterministic():
    a = sql_function_to_spec("series", SERIES_ARGS, "c").to_json_schema()
    b = sql_function_to_spec("series", SERIES_ARGS, "c").to_json_schema()
    assert a == b
    assert json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True)
    assert a is not b


def test_spec_none_comment_is_empty_description():
    assert sql_function_to_spec("f", "", None).description == ""


# ---------------------------------------------------------------------------
# Catalog plumbing
# ---------------------------------------------------------------------------


def test_catalog_sql_is_the_single_source_query():
    assert "pg_get_function_arguments" in PG_FUNCTION_CATALOG_SQL
    assert "obj_description" in PG_FUNCTION_CATALOG_SQL
    assert ":schema" in PG_FUNCTION_CATALOG_SQL
    assert "prokind = 'f'" in PG_FUNCTION_CATALOG_SQL


def test_catalog_rows_project_in_order():
    rows = [
        {"schema": "app", "name": "series", "args": SERIES_ARGS, "comment": "Series."},
        {"schema": "app", "name": "latest", "args": "", "comment": None},
    ]
    specs = catalog_rows_to_specs(rows)
    assert [s.name for s in specs] == ["series", "latest"]
    assert specs[1].inputs == {} and specs[1].description == ""
    assert specs[1].to_json_schema() == {"type": "object", "properties": {}}
