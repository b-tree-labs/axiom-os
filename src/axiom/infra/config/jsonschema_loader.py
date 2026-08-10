# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""JSON Schema entry path for `axiom.infra.config` (ADR-065 PR-1).

Parses a JSON Schema (Draft 2020-12) file and converts each top-level
property into a :class:`axiom.infra.config.registry.Field` registered
through the existing :class:`ConfigRegistry`. The five-verb surface
(``register_schema`` / ``get_value`` / ``write_value`` / ``observe`` /
``lock``) is unchanged; this module is an additive entry.

Per ADR-065:

- The JSON Schema is the language-agnostic floor for adopter sites.
- ``x-reloadable`` per-field annotation is preserved on the ``Field``
  (read back via ``Field.description`` JSON tail until PR-3 plumbs the
  reload contract through the watcher).
- Cross-field validators live in the optional Pydantic ceiling (PR-2).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from axiom.infra.config.registry import (
    Field,
    SchemaError,
    get_registry,
)


# JSON Schema primitive → Python type mapping for the floor.
_JSON_TYPE_TO_PY: dict[str, type] = {
    "string": str,
    "integer": int,
    "number": float,
    "boolean": bool,
    "array": list,
    "object": dict,
}


def _py_type_for(prop_schema: dict[str, Any], prop_name: str) -> type:
    """Resolve the Python type for a JSON Schema property's ``type`` keyword.

    Multi-type / union types degrade to ``object`` so the registry
    doesn't over-constrain; per-field validators in the ceiling
    handle richer cases.
    """
    t = prop_schema.get("type")
    if isinstance(t, str):
        try:
            return _JSON_TYPE_TO_PY[t]
        except KeyError:
            raise SchemaError(
                f"jsonschema property {prop_name!r}: unknown type {t!r}"
            )
    if isinstance(t, list):
        # Union → object floor; ceiling enforces precise shape.
        return object
    if t is None:
        # No `type` keyword (e.g. pure `enum` / `const`) — accept anything.
        return object
    raise SchemaError(
        f"jsonschema property {prop_name!r}: type must be a string or list"
    )


def load_jsonschema(path: Path) -> dict[str, Any]:
    """Read + parse the JSON Schema file. Raises :class:`SchemaError`
    on malformed input."""
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError as exc:
        raise SchemaError(f"jsonschema: cannot read {path}: {exc}") from exc
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise SchemaError(
            f"jsonschema: {path}: invalid JSON: {exc.msg} at line {exc.lineno}"
        ) from exc


def register_schema_from_jsonschema(
    extension: str, schema_path: Path | str
) -> dict[str, Any]:
    """Parse a JSON Schema file and register Field-equivalents.

    Returns the parsed schema dict so callers (e.g. the ``validate``
    skill) can run ``jsonschema.validate`` against the same artifact
    without re-reading the file.

    The schema must be a top-level ``"object"`` whose ``"properties"``
    define the configurable fields. Each property becomes one
    :class:`Field` namespaced as ``"{extension}.{prop_name}"``.

    Recognized per-property keywords:

    - ``type`` → Python type (string/integer/number/boolean/array/object).
    - ``default`` → the Field's default value.
    - ``description`` → carried through to ``Field.description``.
    - ``x-reloadable`` → preserved in the description tail as
      ``"[x-reloadable=true|false]"`` until PR-3 plumbs it into the
      watcher contract. (The keyword stays in the JSON Schema; this is
      only the registry-side breadcrumb.)
    """
    schema_path = Path(schema_path)
    schema = load_jsonschema(schema_path)

    if not isinstance(schema, dict):
        raise SchemaError(
            f"jsonschema {schema_path}: top-level must be an object"
        )
    if schema.get("type") not in (None, "object"):
        raise SchemaError(
            f"jsonschema {schema_path}: top-level type must be 'object'"
        )

    props = schema.get("properties", {})
    if not isinstance(props, dict):
        raise SchemaError(
            f"jsonschema {schema_path}: 'properties' must be an object"
        )

    fields: list[Field] = []
    for prop_name, prop_schema in props.items():
        if not isinstance(prop_schema, dict):
            raise SchemaError(
                f"jsonschema {schema_path}: property {prop_name!r} "
                "must be an object"
            )
        py_type = _py_type_for(prop_schema, prop_name)
        default = prop_schema.get("default")
        description = prop_schema.get("description", "")
        reloadable = prop_schema.get("x-reloadable")
        if reloadable is not None:
            tail = f"[x-reloadable={'true' if reloadable else 'false'}]"
            description = f"{description} {tail}".strip()

        fields.append(
            Field(
                name=f"{extension}.{prop_name}",
                type=py_type,
                default=default,
                classification="internal",
                lockable=True,
                description=description,
            )
        )

    get_registry().register(*fields)
    return schema


__all__ = [
    "load_jsonschema",
    "register_schema_from_jsonschema",
]
