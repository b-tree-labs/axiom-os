# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""TDD tests for T0-4 structured output via tool-use-as-schema.

The pattern: define a synthetic tool whose input schema is the desired
output schema, force the model to call it, and parse ``tool.input`` as
the structured result. Reliable schema adherence; works on any provider
that supports tool-use.
"""

from __future__ import annotations

from typing import TypedDict
from unittest.mock import MagicMock

import pytest

from axiom.infra.gateway import CompletionResponse, ToolUseBlock
from axiom.infra.structured_output import (
    SchemaValidationError,
    StructuredResult,
    schema_to_tool,
    structured_output,
)

# ---------------------------------------------------------------------------
# Schema → tool adaptation
# ---------------------------------------------------------------------------


class ScoreSchema(TypedDict):
    score: int
    rationale: str


class TestSchemaAdapter:
    def test_typeddict_becomes_tool(self):
        tool = schema_to_tool(ScoreSchema)
        assert tool["name"] == "emit_structured_output"
        assert tool["input_schema"]["type"] == "object"
        assert "score" in tool["input_schema"]["properties"]
        assert "rationale" in tool["input_schema"]["properties"]
        assert set(tool["input_schema"]["required"]) == {"score", "rationale"}

    def test_typeddict_type_mapping(self):
        tool = schema_to_tool(ScoreSchema)
        props = tool["input_schema"]["properties"]
        assert props["score"]["type"] == "integer"
        assert props["rationale"]["type"] == "string"

    def test_dict_jsonschema_passthrough(self):
        schema = {
            "type": "object",
            "properties": {"x": {"type": "number"}},
            "required": ["x"],
        }
        tool = schema_to_tool(schema)
        assert tool["input_schema"] == schema

    def test_custom_tool_name(self):
        tool = schema_to_tool(ScoreSchema, tool_name="grade_answer")
        assert tool["name"] == "grade_answer"


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def _fake_gateway(tool_input: dict, tool_name: str = "emit_structured_output") -> MagicMock:
    gw = MagicMock()
    gw.complete_with_tools.return_value = CompletionResponse(
        text="",
        tool_use=[ToolUseBlock(tool_id="toolu_1", name=tool_name, input=tool_input)],
        provider="test",
        model="test",
        success=True,
    )
    return gw


class TestHappyPath:
    def test_returns_structured_result(self):
        gw = _fake_gateway({"score": 8, "rationale": "correct"})
        result = structured_output(
            gateway=gw,
            schema=ScoreSchema,
            messages=[{"role": "user", "content": "grade this"}],
        )
        assert isinstance(result, StructuredResult)
        assert result.value == {"score": 8, "rationale": "correct"}
        assert result.raw == {"score": 8, "rationale": "correct"}
        assert result.tool_call_id == "toolu_1"
        assert result.validation_errors == []

    def test_gateway_receives_single_tool(self):
        gw = _fake_gateway({"score": 8, "rationale": "ok"})
        structured_output(
            gateway=gw, schema=ScoreSchema,
            messages=[{"role": "user", "content": "q"}],
        )
        kwargs = gw.complete_with_tools.call_args.kwargs
        assert len(kwargs["tools"]) == 1
        assert kwargs["tools"][0]["name"] == "emit_structured_output"

    def test_custom_tool_name_flows_through(self):
        gw = _fake_gateway({"score": 8, "rationale": "ok"}, tool_name="grade")
        structured_output(
            gateway=gw, schema=ScoreSchema,
            messages=[{"role": "user", "content": "q"}],
            tool_name="grade",
        )
        kwargs = gw.complete_with_tools.call_args.kwargs
        assert kwargs["tools"][0]["name"] == "grade"


# ---------------------------------------------------------------------------
# Validation + retry
# ---------------------------------------------------------------------------


class TestValidation:
    def test_missing_required_key_triggers_retry(self):
        """First response missing 'rationale' → retry with tightened prompt."""
        gw = MagicMock()
        gw.complete_with_tools.side_effect = [
            CompletionResponse(
                text="",
                tool_use=[ToolUseBlock(tool_id="t1", name="emit_structured_output",
                                         input={"score": 8})],
                success=True,
            ),
            CompletionResponse(
                text="",
                tool_use=[ToolUseBlock(tool_id="t2", name="emit_structured_output",
                                         input={"score": 8, "rationale": "retry"})],
                success=True,
            ),
        ]
        result = structured_output(
            gateway=gw, schema=ScoreSchema,
            messages=[{"role": "user", "content": "q"}],
        )
        assert result.value["rationale"] == "retry"
        assert gw.complete_with_tools.call_count == 2

    def test_retry_exhausted_returns_errors(self):
        gw = MagicMock()
        # Both responses missing rationale
        bad = CompletionResponse(
            text="",
            tool_use=[ToolUseBlock(tool_id="t1", name="emit_structured_output",
                                     input={"score": 8})],
            success=True,
        )
        gw.complete_with_tools.return_value = bad
        with pytest.raises(SchemaValidationError, match="rationale"):
            structured_output(
                gateway=gw, schema=ScoreSchema,
                messages=[{"role": "user", "content": "q"}],
            )
        assert gw.complete_with_tools.call_count == 2  # one + one retry

    def test_no_retry_when_disabled(self):
        gw = MagicMock()
        gw.complete_with_tools.return_value = CompletionResponse(
            text="",
            tool_use=[ToolUseBlock(tool_id="t1", name="emit_structured_output",
                                     input={"score": 8})],
            success=True,
        )
        with pytest.raises(SchemaValidationError):
            structured_output(
                gateway=gw, schema=ScoreSchema,
                messages=[{"role": "user", "content": "q"}],
                max_retries=0,
            )
        assert gw.complete_with_tools.call_count == 1


class TestFailureCases:
    def test_no_tool_use_raises(self):
        gw = MagicMock()
        gw.complete_with_tools.return_value = CompletionResponse(
            text="plain text no tool call", tool_use=[], success=True,
        )
        with pytest.raises(SchemaValidationError, match="tool"):
            structured_output(
                gateway=gw, schema=ScoreSchema,
                messages=[{"role": "user", "content": "q"}],
            )

    def test_provider_failure_propagates(self):
        gw = MagicMock()
        gw.complete_with_tools.return_value = CompletionResponse(
            text="", tool_use=[], success=False, error="provider down",
        )
        with pytest.raises(SchemaValidationError, match="provider"):
            structured_output(
                gateway=gw, schema=ScoreSchema,
                messages=[{"role": "user", "content": "q"}],
            )


# ---------------------------------------------------------------------------
# Dict-schema callers (no TypedDict)
# ---------------------------------------------------------------------------


class TestDictSchema:
    def test_raw_jsonschema_works(self):
        schema = {
            "type": "object",
            "properties": {
                "answer": {"type": "string"},
                "confidence": {"type": "number"},
            },
            "required": ["answer"],
        }
        gw = _fake_gateway({"answer": "yes", "confidence": 0.9})
        result = structured_output(
            gateway=gw, schema=schema,
            messages=[{"role": "user", "content": "q"}],
        )
        assert result.value["answer"] == "yes"
