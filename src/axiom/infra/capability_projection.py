# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0

"""Shared capability projector (ADR-072 / AEOS §4.9).

A registered capability (``SkillSpec``, ADR-056) is the single source of
truth. Its CLI verb, MCP tool, generated SKILL.md, and agent-facing LLM tool
are *projections* of that one capability. This module is the **one** place
the platform converts a capability to surface form — so no surface invents
its own name-mangling or schema translation:

- :func:`capability_to_surface_name` / :func:`surface_to_capability_name` —
  the single dotted ``ns.verb`` ⇄ surface-safe ``ns__verb`` round-trip
  (transports like MCP tool names and LLM function names forbid ``.``).
- :func:`inputs_to_json_schema` — the single ``SkillSpec.inputs`` → JSON
  Schema derivation.
- :func:`approval_category` / :func:`is_read_only` — the READ/WRITE → approval
  decision, read from the capability's ``side_effects`` (never defaulted per
  surface).

Replaces the triplicated manglers (mcp/chat/hooks) and schema generators.
"""

from __future__ import annotations

from axiom.infra.orchestrator.actions import ActionCategory
from axiom.infra.skills import SkillSpec

# Surface-safe separator. Reserved in capability namespaces and verbs.
SEP = "__"

# Shape-string (``SkillSpec.inputs`` values) → JSON Schema type. The one map.
_TYPE_MAP = {
    "str": "string",
    "string": "string",
    "path": "string",
    "int": "integer",
    "integer": "integer",
    "float": "number",
    "number": "number",
    "bool": "boolean",
    "boolean": "boolean",
    "list": "array",
    "array": "array",
    "dict": "object",
    "object": "object",
}


def capability_to_surface_name(name: str) -> str:
    """``press.draft`` → ``press__draft`` (transport/LLM-safe name)."""
    return name.replace(".", SEP)


def surface_to_capability_name(name: str) -> str:
    """``press__draft`` → ``press.draft`` (recover the capability name)."""
    return name.replace(SEP, ".")


def inputs_to_json_schema(inputs: dict[str, str] | None) -> dict:
    """Derive a JSON Schema object from a capability's ``inputs`` shape map.

    Unknown shapes default to ``string``. This is the single derivation every
    surface consumes; none re-authors a schema (AEOS §4.9.2).
    """
    props: dict[str, dict] = {}
    for field_name, shape in (inputs or {}).items():
        json_type = _TYPE_MAP.get(str(shape).strip().lower(), "string")
        props[field_name] = {"type": json_type, "description": f"{field_name} ({shape})"}
    return {"type": "object", "properties": props}


def is_read_only(spec: SkillSpec) -> bool:
    """True only if the capability explicitly declares ``side_effects=False``.

    Undeclared (``None``) is treated as having side effects — conservative, so
    nothing is silently auto-approved before it declares (AEOS §4.9.3).
    """
    return spec.side_effects is False


def approval_category(spec: SkillSpec) -> ActionCategory:
    """READ for side-effect-free capabilities, else WRITE (confirm-gated)."""
    return ActionCategory.READ if is_read_only(spec) else ActionCategory.WRITE


__all__ = [
    "SEP",
    "approval_category",
    "capability_to_surface_name",
    "inputs_to_json_schema",
    "is_read_only",
    "surface_to_capability_name",
]
