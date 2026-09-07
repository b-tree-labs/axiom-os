# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""``input_schema_module`` / inline ``input_schema`` for manifest tools (WS-C).

Spec: ``docs/specs/spec-builtin-mcp-server.md`` §7.2.

A manifest-declared tool advertises a real ``inputSchema`` when its block
names one; the surface hash follows the schema; a broken target degrades
to the freeform blob with a warning; and the schema is the same dict the
chat surface derives for the same inputs (one projector, ADR-072).
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import textwrap
from pathlib import Path

import pytest

from axiom.extensions.builtins.chat.skill_tools import skills_to_tool_definitions
from axiom.extensions.builtins.mcp import cli as mcp_cli
from axiom.extensions.builtins.mcp.aggregation import AggregationRegistry
from axiom.extensions.builtins.mcp.schema_inference import (
    FREEFORM_INPUT_SCHEMA,
    SchemaResolutionError,
    coerce_input_schema,
    load_schema_target,
)
from axiom.extensions.builtins.mcp.skill_tools import skill_tool_contribution
from axiom.infra.capability_projection import inputs_to_json_schema
from axiom.infra.skills import SkillContext, SkillRegistry, SkillResult, SkillSpec

WARN_LOGGER = "axiom.extensions.builtins.mcp.schema_inference"

DICT_SCHEMA = {
    "type": "object",
    "properties": {"start": {"type": "string", "format": "date"}},
    "required": ["start"],
}
INPUTS_MAP = {"start": "date!", "end": "date", "limit": "int"}


@pytest.fixture
def target_module(tmp_path: Path, monkeypatch):
    """Write an importable module under tmp_path and return its dotted name."""
    made: list[str] = []

    def _make(name: str, source: str) -> str:
        module_name = f"schema_target_{name}"
        (tmp_path / f"{module_name}.py").write_text(textwrap.dedent(source), encoding="utf-8")
        monkeypatch.syspath_prepend(str(tmp_path))
        monkeypatch.delitem(sys.modules, module_name, raising=False)
        made.append(module_name)
        return module_name

    yield _make
    for module_name in made:
        sys.modules.pop(module_name, None)


def _manifest(name: str, tool_block_extra: str = "") -> str:
    return textwrap.dedent(
        f'''
        [extension]
        name = "{name}"
        version = "0.0.1"
        description = "ext {name}"
        owner = "axiom-tests"
        aeos_version = "0.1.0"

        [extension.mcp]
        enabled = true
        prefix = "{name}"

        [[extension.provides]]
        kind = "tool"
        name = "series"
        description = "A series tool."

        [[extension.mcp.tool]]
        name = "series"
        '''
    ) + textwrap.dedent(tool_block_extra)


def _tool(surface, name: str):
    return next(t for t in surface.tools if t.name == name)


# ---------------------------------------------------------------------------
# Resolution through the aggregated surface
# ---------------------------------------------------------------------------


def test_dict_schema_target_replaces_blob_and_changes_hash(
    make_extension, target_module, tmp_axiom_home
):
    mod = target_module("dict", f"SCHEMA = {DICT_SCHEMA!r}\n")
    plain = AggregationRegistry(extensions=[make_extension("alpha", _manifest("alpha"))]).build()
    typed = AggregationRegistry(
        extensions=[
            make_extension("alpha", _manifest("alpha", f'input_schema_module = "{mod}:SCHEMA"\n'))
        ]
    ).build()

    assert _tool(plain, "alpha__series").input_schema == FREEFORM_INPUT_SCHEMA
    assert _tool(typed, "alpha__series").input_schema == DICT_SCHEMA
    assert typed.content_hash != plain.content_hash


def test_callable_target_is_invoked(make_extension, target_module, tmp_axiom_home):
    mod = target_module(
        "callable",
        f"""
        def build_schema():
            return {DICT_SCHEMA!r}
        """,
    )
    surface = AggregationRegistry(
        extensions=[
            make_extension(
                "alpha", _manifest("alpha", f'input_schema_module = "{mod}:build_schema"\n')
            )
        ]
    ).build()
    assert _tool(surface, "alpha__series").input_schema == DICT_SCHEMA


def test_inputs_mapping_target_goes_through_the_one_projector(
    make_extension, target_module, tmp_axiom_home
):
    mod = target_module("inputs", f"INPUTS = {INPUTS_MAP!r}\n")
    surface = AggregationRegistry(
        extensions=[
            make_extension("alpha", _manifest("alpha", f'input_schema_module = "{mod}:INPUTS"\n'))
        ]
    ).build()
    schema = _tool(surface, "alpha__series").input_schema
    assert schema == inputs_to_json_schema(INPUTS_MAP)
    assert schema["properties"]["limit"]["type"] == "integer"
    assert schema["properties"]["start"]["format"] == "date"
    assert schema["required"] == ["start"]


def test_module_without_attr_uses_conventional_name(make_extension, target_module, tmp_axiom_home):
    mod = target_module("conventional", f"INPUT_SCHEMA = {INPUTS_MAP!r}\n")
    surface = AggregationRegistry(
        extensions=[make_extension("alpha", _manifest("alpha", f'input_schema_module = "{mod}"\n'))]
    ).build()
    assert _tool(surface, "alpha__series").input_schema == inputs_to_json_schema(INPUTS_MAP)


def test_pydantic_model_target(make_extension, target_module, tmp_axiom_home):
    mod = target_module(
        "pydantic",
        """
        from pydantic import BaseModel

        class SeriesArgs(BaseModel):
            start: str
            limit: int = 100
        """,
    )
    surface = AggregationRegistry(
        extensions=[
            make_extension(
                "alpha", _manifest("alpha", f'input_schema_module = "{mod}:SeriesArgs"\n')
            )
        ]
    ).build()
    schema = _tool(surface, "alpha__series").input_schema
    assert schema["type"] == "object"
    assert set(schema["properties"]) == {"start", "limit"}
    assert schema["required"] == ["start"]


def test_sql_capability_record_target(make_extension, target_module, tmp_axiom_home):
    """What a catalog generator feeds: a ``CapabilityInputs`` from a SQL signature."""
    mod = target_module(
        "sql",
        """
        from axiom.infra.sql_capability import sql_function_to_spec

        INPUT_SCHEMA = sql_function_to_spec(
            "series",
            "p_start date, p_end date DEFAULT NULL, p_limit integer DEFAULT 100",
            "Rows between two dates.",
        )
        """,
    )
    surface = AggregationRegistry(
        extensions=[make_extension("alpha", _manifest("alpha", f'input_schema_module = "{mod}"\n'))]
    ).build()
    schema = _tool(surface, "alpha__series").input_schema
    assert schema["required"] == ["start"]
    assert schema["properties"]["end"] == {
        "type": "string",
        "format": "date",
        "description": "end (date)",
    }
    assert schema["properties"]["limit"]["type"] == "integer"


def test_inline_input_schema_table(make_extension, tmp_axiom_home):
    ext = make_extension(
        "alpha",
        _manifest(
            "alpha",
            """
            [extension.mcp.tool.input_schema]
            type = "object"
            additionalProperties = false

            [extension.mcp.tool.input_schema.properties.start]
            type = "string"
            """,
        ),
    )
    surface = AggregationRegistry(extensions=[ext]).build()
    assert _tool(surface, "alpha__series").input_schema == {
        "type": "object",
        "additionalProperties": False,
        "properties": {"start": {"type": "string"}},
    }


def test_inline_shape_map_is_projected(make_extension, tmp_axiom_home):
    ext = make_extension(
        "alpha",
        _manifest("alpha", 'input_schema = { start = "date!", limit = "int" }\n'),
    )
    surface = AggregationRegistry(extensions=[ext]).build()
    assert _tool(surface, "alpha__series").input_schema == inputs_to_json_schema(
        {"start": "date!", "limit": "int"}
    )


def test_inline_wins_over_module(make_extension, target_module, tmp_axiom_home):
    mod = target_module("loser", f"SCHEMA = {DICT_SCHEMA!r}\n")
    ext = make_extension(
        "alpha",
        _manifest(
            "alpha",
            f'input_schema_module = "{mod}:SCHEMA"\ninput_schema = {{ limit = "int" }}\n',
        ),
    )
    surface = AggregationRegistry(extensions=[ext]).build()
    assert _tool(surface, "alpha__series").input_schema == inputs_to_json_schema({"limit": "int"})


# ---------------------------------------------------------------------------
# Degradation: never break the surface
# ---------------------------------------------------------------------------


def test_missing_module_falls_back_to_blob_with_warning(make_extension, tmp_axiom_home, caplog):
    plain = AggregationRegistry(extensions=[make_extension("alpha", _manifest("alpha"))]).build()
    with caplog.at_level(logging.WARNING, logger=WARN_LOGGER):
        broken = AggregationRegistry(
            extensions=[
                make_extension(
                    "alpha",
                    _manifest("alpha", 'input_schema_module = "no.such.module_xyz:SCHEMA"\n'),
                )
            ]
        ).build()
    assert _tool(broken, "alpha__series").input_schema == FREEFORM_INPUT_SCHEMA
    assert "no.such.module_xyz" in caplog.text
    assert "freeform" in caplog.text
    # A broken target advertises exactly what an undeclared one does: no drift.
    assert broken.content_hash == plain.content_hash


def test_missing_attribute_falls_back_with_warning(
    make_extension, target_module, tmp_axiom_home, caplog
):
    mod = target_module("noattr", "OTHER = 1\n")
    with caplog.at_level(logging.WARNING, logger=WARN_LOGGER):
        surface = AggregationRegistry(
            extensions=[
                make_extension(
                    "alpha", _manifest("alpha", f'input_schema_module = "{mod}:SCHEMA"\n')
                )
            ]
        ).build()
    assert _tool(surface, "alpha__series").input_schema == FREEFORM_INPUT_SCHEMA
    assert "SCHEMA" in caplog.text


def test_unsupported_target_falls_back_with_warning(
    make_extension, target_module, tmp_axiom_home, caplog
):
    mod = target_module("weird", "SCHEMA = 42\n")
    with caplog.at_level(logging.WARNING, logger=WARN_LOGGER):
        surface = AggregationRegistry(
            extensions=[
                make_extension(
                    "alpha", _manifest("alpha", f'input_schema_module = "{mod}:SCHEMA"\n')
                )
            ]
        ).build()
    assert _tool(surface, "alpha__series").input_schema == FREEFORM_INPUT_SCHEMA
    assert "unsupported schema target" in caplog.text


# ---------------------------------------------------------------------------
# Existing manifests: unchanged
# ---------------------------------------------------------------------------


def test_manifest_without_field_is_unchanged(make_extension, tmp_axiom_home):
    first = AggregationRegistry(extensions=[make_extension("alpha", _manifest("alpha"))]).build()
    second = AggregationRegistry(extensions=[make_extension("alpha", _manifest("alpha"))]).build()
    tool = _tool(first, "alpha__series")
    assert tool.input_schema == FREEFORM_INPUT_SCHEMA
    assert first.content_hash == second.content_hash
    cached = first.to_dict()["tools"]
    assert {
        "name": tool.name,
        "description": tool.description,
        "input_schema": FREEFORM_INPUT_SCHEMA,
    } in cached


def test_hash_follows_schema_content_alone(make_extension, target_module, tmp_axiom_home):
    """Same name + description, different schema → different hash."""
    a = target_module("hash_a", 'INPUT_SCHEMA = {"start": "date!"}\n')
    b = target_module("hash_b", 'INPUT_SCHEMA = {"start": "date!", "limit": "int"}\n')
    s_a = AggregationRegistry(
        extensions=[make_extension("alpha", _manifest("alpha", f'input_schema_module = "{a}"\n'))]
    ).build()
    s_b = AggregationRegistry(
        extensions=[make_extension("alpha", _manifest("alpha", f'input_schema_module = "{b}"\n'))]
    ).build()
    assert _tool(s_a, "alpha__series").description == _tool(s_b, "alpha__series").description
    assert s_a.content_hash != s_b.content_hash


# ---------------------------------------------------------------------------
# One projector: chat and MCP agree
# ---------------------------------------------------------------------------


def test_chat_and_mcp_project_the_same_schema(
    make_extension, target_module, tmp_axiom_home, tmp_path
):
    reg = SkillRegistry()
    reg.register_skill(
        SkillSpec(
            name="alpha.series",
            fn=lambda p, c: SkillResult(ok=True),
            inputs=dict(INPUTS_MAP),
            surfaces=("cli", "mcp", "agent_tool"),
        )
    )
    chat_params = {t.name: t.parameters for t in skills_to_tool_definitions(reg)}["alpha__series"]

    ctx_factory = lambda: SkillContext(  # noqa: E731
        registry=reg, state_dir=tmp_path, logger=logging.getLogger("t")
    )
    registry_schema = {
        t.name: t.input_schema for t in skill_tool_contribution(reg, ctx_factory=ctx_factory).tools
    }["axiom_alpha__series"]

    mod = target_module("agree", f"INPUT_SCHEMA = {INPUTS_MAP!r}\n")
    manifest_schema = _tool(
        AggregationRegistry(
            extensions=[
                make_extension("beta", _manifest("beta", f'input_schema_module = "{mod}"\n'))
            ]
        ).build(),
        "beta__series",
    ).input_schema

    assert chat_params == registry_schema == manifest_schema
    assert json.dumps(chat_params, sort_keys=True) == json.dumps(manifest_schema, sort_keys=True)


# ---------------------------------------------------------------------------
# CLI: inspect shows the resolved schema
# ---------------------------------------------------------------------------


def test_inspect_prints_resolved_schema(
    make_extension, target_module, tmp_axiom_home, monkeypatch, capsys
):
    mod = target_module("inspect", f"INPUT_SCHEMA = {INPUTS_MAP!r}\n")
    surface = AggregationRegistry(
        extensions=[make_extension("alpha", _manifest("alpha", f'input_schema_module = "{mod}"\n'))]
    ).build()
    monkeypatch.setattr(mcp_cli, "_load_or_build_surface", lambda: surface)

    rc = mcp_cli._cmd_inspect(argparse.Namespace(tool="alpha__series"))

    out = capsys.readouterr().out
    assert rc == 0
    printed = json.loads(out.split("input_schema:", 1)[1])
    assert printed == inputs_to_json_schema(INPUTS_MAP)


# ---------------------------------------------------------------------------
# Resolver unit behaviour
# ---------------------------------------------------------------------------


def test_load_schema_target_rejects_empty_and_unknown():
    with pytest.raises(SchemaResolutionError):
        load_schema_target("")
    with pytest.raises(SchemaResolutionError):
        load_schema_target("no.such.module_xyz")


def test_coerce_json_schema_dict_passes_through_untouched():
    schema = {"type": "object", "properties": {"x": {"type": "integer"}}}
    out = coerce_input_schema(schema)
    assert out == schema
    assert out is not schema  # a copy: the surface never aliases a module global


def test_coerce_skillspec_uses_its_inputs():
    spec = SkillSpec(name="a.b", fn=lambda p, c: None, inputs={"limit": "int"})
    assert coerce_input_schema(spec) == inputs_to_json_schema({"limit": "int"})


def test_coerce_empty_mapping_is_a_no_input_tool():
    assert coerce_input_schema({}) == {"type": "object", "properties": {}}
