# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""T0-4 structured output via tool-use-as-schema.

Pattern: define a synthetic tool whose ``input_schema`` is the desired
output schema, force the model to call it, parse ``tool.input`` as the
structured result. The frontier way to get reliable JSON adherence —
works on any provider that supports tool-use.

Usage::

    from typing import TypedDict
    from axiom.infra.structured_output import structured_output

    class ScoreSchema(TypedDict):
        score: int
        rationale: str

    result = structured_output(
        gateway=gw,
        schema=ScoreSchema,
        messages=[{"role": "user", "content": "grade this answer: ..."}],
        system="You are a strict grader.",
    )
    print(result.value["score"], result.value["rationale"])

Supported schema inputs:
    - ``TypedDict`` subclass — auto-converted to JSONSchema
    - ``dict`` — passed through as JSONSchema
    - Pydantic ``BaseModel`` — auto-detected if pydantic is installed

Returns a :class:`StructuredResult` wrapper exposing ``.value``,
``.raw``, ``.tool_call_id``, ``.usage``, and ``.validation_errors``.

Retry: on schema validation failure, the helper re-invokes the gateway
once with a tightened system message ("your previous output failed
validation: ...; try again"). ``max_retries=0`` disables.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from axiom.infra.gateway import CompletionResponse

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class SchemaValidationError(Exception):
    """Raised when structured output cannot be produced after retries."""


# ---------------------------------------------------------------------------
# Return type
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class StructuredResult:
    """Validated structured output from one gateway call."""

    value: dict[str, Any]
    raw: dict[str, Any]
    tool_call_id: str = ""
    usage: dict[str, int] = field(default_factory=dict)
    validation_errors: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Schema adapter
# ---------------------------------------------------------------------------


_TYPE_MAP = {
    int: "integer",
    float: "number",
    str: "string",
    bool: "boolean",
    list: "array",
    dict: "object",
}


def _typeddict_to_jsonschema(td_cls: type) -> dict[str, Any]:
    import typing

    # Resolves PEP-563 string annotations to real types.
    try:
        annotations = typing.get_type_hints(td_cls)
    except Exception:
        annotations = getattr(td_cls, "__annotations__", {})
    required = list(getattr(td_cls, "__required_keys__", annotations.keys()))
    properties: dict[str, Any] = {}
    for name, typ in annotations.items():
        json_type = _TYPE_MAP.get(typ, "string")
        properties[name] = {"type": json_type}
    return {
        "type": "object",
        "properties": properties,
        "required": required,
    }


def _is_pydantic_model(obj: Any) -> bool:
    try:
        from pydantic import BaseModel  # noqa: F401
    except ImportError:
        return False
    try:
        from pydantic import BaseModel

        return isinstance(obj, type) and issubclass(obj, BaseModel)
    except Exception:
        return False


def _pydantic_to_jsonschema(model_cls: type) -> dict[str, Any]:
    """Convert a Pydantic v2 BaseModel to an inline JSONSchema."""
    return model_cls.model_json_schema()  # type: ignore[attr-defined]


def schema_to_tool(
    schema: type | dict[str, Any],
    tool_name: str = "emit_structured_output",
    description: str = (
        "Emit the final structured output. Call this tool exactly once with "
        "the complete result; do not emit any other tool calls or plain text."
    ),
) -> dict[str, Any]:
    """Convert a schema (TypedDict, dict, or Pydantic model) to a tool dict."""
    if isinstance(schema, dict):
        input_schema = schema
    elif _is_pydantic_model(schema):
        input_schema = _pydantic_to_jsonschema(schema)  # type: ignore[arg-type]
    elif isinstance(schema, type):
        input_schema = _typeddict_to_jsonschema(schema)
    else:
        raise TypeError(
            f"schema must be TypedDict, dict (JSONSchema), or Pydantic "
            f"BaseModel subclass; got {type(schema).__name__}"
        )
    return {
        "name": tool_name,
        "description": description,
        "input_schema": input_schema,
    }


# ---------------------------------------------------------------------------
# Validation (lightweight presence + type coercion)
# ---------------------------------------------------------------------------


def _validate_against_schema(
    value: dict[str, Any], schema: dict[str, Any]
) -> list[str]:
    errors: list[str] = []
    required = schema.get("required", [])
    properties = schema.get("properties", {})

    for key in required:
        if key not in value:
            errors.append(f"missing required key: {key!r}")

    for key, prop_schema in properties.items():
        if key not in value:
            continue
        expected = prop_schema.get("type")
        got = value[key]
        if expected == "integer" and not isinstance(got, int):
            errors.append(f"{key!r} must be integer, got {type(got).__name__}")
        elif expected == "number" and not isinstance(got, (int, float)):
            errors.append(f"{key!r} must be number, got {type(got).__name__}")
        elif expected == "string" and not isinstance(got, str):
            errors.append(f"{key!r} must be string, got {type(got).__name__}")
        elif expected == "boolean" and not isinstance(got, bool):
            errors.append(f"{key!r} must be boolean, got {type(got).__name__}")
        elif expected == "array" and not isinstance(got, list):
            errors.append(f"{key!r} must be array, got {type(got).__name__}")
        elif expected == "object" and not isinstance(got, dict):
            errors.append(f"{key!r} must be object, got {type(got).__name__}")
    return errors


# ---------------------------------------------------------------------------
# Main API
# ---------------------------------------------------------------------------


def structured_output(
    *,
    gateway,
    schema: type | dict[str, Any],
    messages: list[dict[str, Any]],
    system: str = "",
    tool_name: str = "emit_structured_output",
    max_tokens: int = 2048,
    task: str = "extraction",
    routing_tier: str = "any",
    routing_tags: set[str] | None = None,
    max_retries: int = 1,
) -> StructuredResult:
    """Invoke the gateway and return a schema-validated structured result.

    Args:
        gateway: Any object with a ``complete_with_tools`` method matching
            :class:`axiom.infra.gateway.Gateway`.
        schema: Output schema — TypedDict, JSONSchema dict, or Pydantic model.
        messages: Conversation prefix.
        system: Optional system prompt.
        tool_name: Override the synthetic tool name. Default works for
            most cases; rename if you want tool traces to carry task-
            specific semantics (e.g. ``"grade_answer"``).
        max_tokens: Budget for the generation.
        task / routing_tier / routing_tags: Passed through to the gateway.
        max_retries: Number of validation-failure retries. Default 1.
            Set 0 to fail fast on the first invalid response.

    Returns:
        :class:`StructuredResult` on success.

    Raises:
        :class:`SchemaValidationError` if no valid output is produced
        within ``max_retries + 1`` attempts, if the provider fails, or
        if no tool_use block is returned.
    """
    tool = schema_to_tool(schema, tool_name=tool_name)
    input_schema = tool["input_schema"]

    effective_system = system
    last_errors: list[str] = []

    for attempt in range(max_retries + 1):
        response: CompletionResponse = gateway.complete_with_tools(
            messages=messages,
            system=effective_system,
            tools=[tool],
            max_tokens=max_tokens,
            task=task,
            routing_tier=routing_tier,
            routing_tags=routing_tags,
        )

        if not response.success:
            raise SchemaValidationError(
                f"provider call failed: {response.error or 'unknown'}"
            )

        tool_calls = [t for t in response.tool_use if t.name == tool_name]
        if not tool_calls:
            if attempt < max_retries:
                effective_system = _augment_system_retry(
                    system,
                    ["no tool call emitted — you must call the "
                     f"{tool_name!r} tool exactly once"],
                )
                continue
            raise SchemaValidationError(
                f"model did not call the {tool_name!r} tool"
            )

        tool_call = tool_calls[0]
        value = tool_call.input or {}

        errors = _validate_against_schema(value, input_schema)
        if not errors:
            return StructuredResult(
                value=value,
                raw=value,
                tool_call_id=tool_call.tool_id,
                usage={
                    "input_tokens": response.input_tokens,
                    "output_tokens": response.output_tokens,
                    "cache_read_tokens": response.cache_read_tokens,
                },
                validation_errors=[],
            )

        last_errors = errors
        if attempt < max_retries:
            effective_system = _augment_system_retry(system, errors)
            log.info("structured_output attempt %d failed: %s", attempt + 1, errors)
            continue

    raise SchemaValidationError(
        "schema validation failed after "
        f"{max_retries + 1} attempt(s): {last_errors}"
    )


def _augment_system_retry(base_system: str, errors: list[str]) -> str:
    """Append a targeted re-ask note to the system prompt."""
    note = (
        "\n\nYour previous tool call failed schema validation:\n  - "
        + "\n  - ".join(errors)
        + "\n\nCall the tool again with all required fields and correct types."
    )
    return (base_system or "") + note
