# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Input-schema inference for manifest-declared MCP tools (WS-C).

Spec: ``docs/specs/spec-builtin-mcp-server.md`` §7.2.

Registry-backed tools already get their ``inputSchema`` from the shared
projector (ADR-072/073). Manifest-declared ``[[extension.mcp.tool]]``
blocks resolve theirs here, in order:

1. an inline ``input_schema`` table in the block;
2. ``input_schema_module`` — ``pkg.mod:attr``, or ``pkg.mod`` with the
   conventional ``INPUT_SCHEMA`` attribute — imported lazily;
3. otherwise the freeform ``{"type": "object", "additionalProperties": true}``.

Whatever a target resolves to is coerced through one rule set
(:func:`coerce_input_schema`): a JSON Schema dict passes through; a
``SkillSpec.inputs``-style shape map (all-string values, no JSON-Schema
keywords) is projected through ``inputs_to_json_schema`` — the same call
the CLI, chat and registry-MCP surfaces make; a callable is invoked; an
object with ``to_json_schema()`` (a ``CapabilityInputs`` record from
``axiom.infra.sql_capability``), ``model_json_schema()`` (pydantic) or
``.inputs`` (a ``SkillSpec``) is asked. A missing or broken target logs a
warning and falls back to the freeform schema: the surface never breaks.
"""

from __future__ import annotations

import copy
import importlib
import logging
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from axiom.infra.capability_projection import inputs_to_json_schema

if TYPE_CHECKING:
    from axiom.extensions.builtins.mcp.manifest_schema import MCPToolDecl

log = logging.getLogger(__name__)

# The pre-WS-C blob: accept anything. Still the fallback.
FREEFORM_INPUT_SCHEMA: dict[str, Any] = {"type": "object", "additionalProperties": True}

# Attribute names tried, in order, when ``input_schema_module`` has no ``:attr``.
CONVENTIONAL_ATTRS: tuple[str, ...] = ("INPUT_SCHEMA", "input_schema")

_JSON_SCHEMA_TYPES = frozenset(
    {"object", "string", "integer", "number", "boolean", "array", "null"}
)
_JSON_SCHEMA_KEYWORDS = frozenset(
    {
        "$schema",
        "$ref",
        "$defs",
        "properties",
        "required",
        "additionalProperties",
        "items",
        "enum",
        "oneOf",
        "anyOf",
        "allOf",
    }
)
_MAX_CALL_DEPTH = 2


class SchemaResolutionError(Exception):
    """A schema target could not be imported, found or coerced."""


def freeform_input_schema() -> dict[str, Any]:
    """A fresh copy of the freeform fallback (tools must not share one dict)."""
    return dict(FREEFORM_INPUT_SCHEMA)


def load_schema_target(path: str) -> Any:
    """Import ``pkg.mod:attr`` (or ``pkg.mod`` + conventional attribute)."""
    module_path, _, attr = path.strip().partition(":")
    if not module_path:
        raise SchemaResolutionError("empty module path")
    try:
        module = importlib.import_module(module_path)
    except Exception as exc:  # noqa: BLE001 — any import failure is a resolution failure
        raise SchemaResolutionError(
            f"could not import {module_path!r}: {type(exc).__name__}: {exc}"
        ) from exc
    if attr:
        try:
            return getattr(module, attr)
        except AttributeError:
            raise SchemaResolutionError(
                f"module {module_path!r} has no attribute {attr!r}"
            ) from None
    for candidate in CONVENTIONAL_ATTRS:
        if hasattr(module, candidate):
            return getattr(module, candidate)
    raise SchemaResolutionError(
        f"module {module_path!r} defines none of {', '.join(CONVENTIONAL_ATTRS)}; "
        "name the attribute as 'pkg.mod:attr'"
    )


def is_inputs_map(obj: Mapping[str, Any]) -> bool:
    """True when a mapping is a ``SkillSpec.inputs`` shape map, not a JSON Schema."""
    if "type" in obj and obj["type"] in _JSON_SCHEMA_TYPES:
        return False
    if any(key in _JSON_SCHEMA_KEYWORDS for key in obj):
        return False
    return all(isinstance(value, str) for value in obj.values())


def coerce_input_schema(obj: Any, *, _depth: int = 0) -> dict[str, Any]:
    """Turn whatever a schema target resolved to into a JSON Schema dict."""
    if isinstance(obj, Mapping):
        if is_inputs_map(obj):
            return inputs_to_json_schema(dict(obj))
        return copy.deepcopy(dict(obj))
    model_schema = getattr(obj, "model_json_schema", None)
    if callable(model_schema):  # pydantic model class or instance
        return coerce_input_schema(model_schema(), _depth=_depth + 1)
    to_schema = getattr(obj, "to_json_schema", None)
    if callable(to_schema):  # CapabilityInputs and friends
        return coerce_input_schema(to_schema(), _depth=_depth + 1)
    inputs = getattr(obj, "inputs", None)
    if isinstance(inputs, Mapping):  # SkillSpec-like
        return inputs_to_json_schema(dict(inputs))
    if callable(obj):
        if _depth >= _MAX_CALL_DEPTH:
            raise SchemaResolutionError("schema target keeps returning callables")
        return coerce_input_schema(obj(), _depth=_depth + 1)
    raise SchemaResolutionError(
        f"unsupported schema target of type {type(obj).__name__}; expected a JSON "
        "Schema dict, an inputs shape map, a callable, a pydantic model, or an "
        "object with to_json_schema()/inputs"
    )


def resolve_input_schema(decl: MCPToolDecl, *, extension_name: str) -> dict[str, Any]:
    """The ``inputSchema`` for one manifest tool; never raises."""
    inline = getattr(decl, "input_schema", None)
    if isinstance(inline, Mapping):
        try:
            return coerce_input_schema(inline)
        except SchemaResolutionError as exc:
            log.warning(
                "mcp: %s.%s: inline input_schema rejected (%s); using freeform schema",
                extension_name,
                decl.name,
                exc,
            )
            return freeform_input_schema()
    target = getattr(decl, "input_schema_module", "") or ""
    if target:
        try:
            return coerce_input_schema(load_schema_target(target))
        except Exception as exc:  # noqa: BLE001 — a bad target must not break the surface
            log.warning(
                "mcp: %s.%s: could not resolve input_schema_module %r (%s: %s); "
                "using freeform schema",
                extension_name,
                decl.name,
                target,
                type(exc).__name__,
                exc,
            )
    return freeform_input_schema()


__all__ = [
    "CONVENTIONAL_ATTRS",
    "FREEFORM_INPUT_SCHEMA",
    "SchemaResolutionError",
    "coerce_input_schema",
    "freeform_input_schema",
    "is_inputs_map",
    "load_schema_target",
    "resolve_input_schema",
]
